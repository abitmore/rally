# Licensed to Elasticsearch B.V. under one or more contributor
# license agreements. See the NOTICE file distributed with
# this work for additional information regarding copyright
# ownership. Elasticsearch B.V. licenses this file to you under
# the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# 	http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
from __future__ import annotations

import collections
import datetime
import glob
import json
import logging
import math
import os
import pickle
import random
import statistics
import sys
import uuid
import zlib
from enum import Enum, IntEnum

import tabulate

from esrally import client, config, exceptions, paths, time, types, version
from esrally.utils import console, convert, io, pretty, versions


class EsClient:
    """
    Provides a stripped-down client interface that is easier to exchange for testing
    """

    def __init__(self, client, cluster_version=None):
        self._client = client
        self.logger = logging.getLogger(__name__)
        self._cluster_version = cluster_version
        self.retryable_status_codes = [502, 503, 504, 429]

    def get_template(self, name):
        return self.guarded(self._client.indices.get_index_template, name=name)

    def put_template(self, name, template):
        tmpl = json.loads(template)
        return self.guarded(self._client.indices.put_index_template, name=name, **tmpl)

    def template_exists(self, name):
        return self.guarded(self._client.indices.exists_index_template, name=name)

    def delete_by_query(self, index, body):
        return self.guarded(self._client.delete_by_query, index=index, body=body)

    def delete(self, index, id):
        # ignore 404 status code (NotFoundError) when index does not exist
        return self.guarded(self._client.delete, index=index, id=id, ignore=404)

    def get_index(self, name):
        return self.guarded(self._client.indices.get, name=name)

    def create_index(self, index):
        # ignore 400 status code (BadRequestError) when index already exists
        return self.guarded(self._client.indices.create, index=index, ignore=400)

    def exists(self, index):
        return self.guarded(self._client.indices.exists, index=index)

    def refresh(self, index):
        return self.guarded(self._client.indices.refresh, index=index)

    def bulk_index(self, index, items):
        # pylint: disable=import-outside-toplevel
        import elasticsearch.helpers

        self.guarded(elasticsearch.helpers.bulk, self._client, items, index=index, chunk_size=5000)

    def index(self, index, item, id=None):
        doc = {"_source": item}
        if id:
            doc["_id"] = id
        self.bulk_index(index, [doc])

    def search(self, index, body):
        return self.guarded(self._client.search, index=index, body=body)

    def guarded(self, target, *args, **kwargs):
        # pylint: disable=import-outside-toplevel
        import elasticsearch
        import elasticsearch.helpers
        from elastic_transport import ApiError, TransportError

        max_execution_count = 10
        execution_count = 0

        while execution_count <= max_execution_count:
            time_to_sleep = 2**execution_count + random.random()
            execution_count += 1

            try:
                return target(*args, **kwargs)
            except elasticsearch.exceptions.ConnectionTimeout as e:
                if execution_count <= max_execution_count:
                    self.logger.debug(
                        "Connection timeout [%s] in attempt [%d/%d]. Sleeping for [%f] seconds.",
                        e.message,
                        execution_count,
                        max_execution_count,
                        time_to_sleep,
                    )
                    time.sleep(time_to_sleep)
                else:
                    operation = target.__name__
                    self.logger.exception("Connection timeout while running [%s] (retried %d times).", operation, max_execution_count)
                    node = self._client.transport.node_pool.get()
                    msg = (
                        "A connection timeout occurred while running the operation [%s] against your Elasticsearch metrics store on "
                        "host [%s] at port [%s]." % (operation, node.host, node.port)
                    )
                    raise exceptions.RallyError(msg)
            except elasticsearch.exceptions.ConnectionError as e:
                if execution_count <= max_execution_count:
                    self.logger.debug(
                        "Connection error [%s] in attempt [%d/%d]. Sleeping for [%f] seconds.",
                        e.message,
                        execution_count,
                        max_execution_count,
                        time_to_sleep,
                    )
                    time.sleep(time_to_sleep)
                else:
                    node = self._client.transport.node_pool.get()
                    msg = (
                        "Could not connect to your Elasticsearch metrics store. Please check that it is running on host [%s] at port [%s]"
                        " or fix the configuration in [%s]." % (node.host, node.port, config.ConfigFile().location)
                    )
                    self.logger.exception(msg)
                    # connection errors doesn't neccessarily mean it's during setup
                    raise exceptions.RallyError(msg)
            except elasticsearch.exceptions.AuthenticationException:
                # we know that it is just one host (see EsClientFactory)
                node = self._client.transport.node_pool.get()
                msg = (
                    "The configured user could not authenticate against your Elasticsearch metrics store running on host [%s] at "
                    "port [%s] (wrong password?). Please fix the configuration in [%s]."
                    % (node.host, node.port, config.ConfigFile().location)
                )
                self.logger.exception(msg)
                raise exceptions.SystemSetupError(msg)
            except elasticsearch.exceptions.AuthorizationException:
                node = self._client.transport.node_pool.get()
                msg = (
                    "The configured user does not have enough privileges to run the operation [%s] against your Elasticsearch metrics "
                    "store running on host [%s] at port [%s]. Please adjust your x-pack configuration or specify a user with enough "
                    "privileges in the configuration in [%s]." % (target.__name__, node.host, node.port, config.ConfigFile().location)
                )
                self.logger.exception(msg)
                raise exceptions.SystemSetupError(msg)
            except elasticsearch.helpers.BulkIndexError as e:
                for err in e.errors:
                    err_type = err.get("index", {}).get("error", {}).get("type", None)
                    if err.get("index", {}).get("status", None) not in self.retryable_status_codes:
                        msg = f"Unretryable error encountered when sending metrics to remote metrics store: [{err_type}]"
                        self.logger.exception("%s - Full error(s) [%s]", msg, str(e.errors))
                        raise exceptions.RallyError(msg)

                if execution_count <= max_execution_count:
                    self.logger.debug(
                        "Error in sending metrics to remote metrics store [%s] in attempt [%d/%d]. Sleeping for [%f] seconds.",
                        e,
                        execution_count,
                        max_execution_count,
                        time_to_sleep,
                    )
                    time.sleep(time_to_sleep)
                else:
                    msg = f"Failed to send metrics to remote metrics store: [{e.errors}]"
                    self.logger.exception("%s - Full error(s) [%s]", msg, str(e.errors))
                    raise exceptions.RallyError(msg)
            except ApiError as e:
                if e.status_code in self.retryable_status_codes and execution_count <= max_execution_count:
                    self.logger.debug(
                        "%s (code: %d) in attempt [%d/%d]. Sleeping for [%f] seconds.",
                        e.error,
                        e.status_code,
                        execution_count,
                        max_execution_count,
                        time_to_sleep,
                    )
                    time.sleep(time_to_sleep)
                else:
                    node = self._client.transport.node_pool.get()
                    msg = (
                        "An error [%s] occurred while running the operation [%s] against your Elasticsearch metrics store on host [%s] "
                        "at port [%s]." % (e.error, target.__name__, node.host, node.port)
                    )
                    self.logger.exception(msg)
                    # this does not necessarily mean it's a system setup problem...
                    raise exceptions.RallyError(msg)
            except TransportError as e:
                node = self._client.transport.node_pool.get()

                if e.errors:
                    err = e.errors
                else:
                    err = e
                msg = (
                    "Transport error(s) [%s] occurred while running the operation [%s] against your Elasticsearch metrics store on "
                    "host [%s] at port [%s]." % (err, target.__name__, node.host, node.port)
                )
                self.logger.exception(msg)
                # this does not necessarily mean it's a system setup problem...
                raise exceptions.RallyError(msg)


DATASTORE_API_KEY: str = os.environ.get("RALLY_REPORTING_DATASTORE_API_KEY", "")
DATASTORE_USER: str = os.environ.get("RALLY_REPORTING_DATASTORE_USER", "")
DATASTORE_PASSWORD: str = os.environ.get("RALLY_REPORTING_DATASTORE_PASSWORD", "")


class EsClientFactory:
    """
    Abstracts how the Elasticsearch client is created. Intended for testing.
    """

    def __init__(self, cfg: types.Config, client_factory=client.EsClientFactory):
        self._config = cfg
        host = self._config.opts("reporting", "datastore.host")
        port = self._config.opts("reporting", "datastore.port")
        hosts = [{"host": host, "port": port}]
        secure = convert.to_bool(self._config.opts("reporting", "datastore.secure"))
        user: str = DATASTORE_USER or self._config.opts("reporting", "datastore.user", default_value="", mandatory=False)
        api_key: str = DATASTORE_API_KEY or self._config.opts("reporting", "datastore.api_key", default_value="", mandatory=False)
        password: str = DATASTORE_PASSWORD or self._config.opts("reporting", "datastore.password", default_value="", mandatory=False)
        distribution_version = None
        distribution_flavor = None

        # Resolve the authentication method.
        if user:
            if api_key:
                raise exceptions.ConfigError(
                    "Both basic authentication (username/password) and API key are provided. Please provide only one authentication method."
                )
            if not password:
                raise exceptions.ConfigError(
                    "No password configured through [reporting] configuration or "
                    "RALLY_REPORTING_DATASTORE_PASSWORD environment variable."
                )

        verify = self._config.opts("reporting", "datastore.ssl.verification_mode", default_value="full", mandatory=False) != "none"
        ca_path = self._config.opts("reporting", "datastore.ssl.certificate_authorities", default_value=None, mandatory=False)
        self.probe_version = self._config.opts("reporting", "datastore.probe.cluster_version", default_value=True, mandatory=False)

        # Instead of duplicating code, we're just adapting the metrics store specific properties to match the regular client options.

        client_options = {
            "use_ssl": secure,
            "verify_certs": verify,
            "timeout": 120,
        }
        if ca_path:
            client_options["ca_certs"] = ca_path
        if user:
            client_options["basic_auth_user"] = user
            client_options["basic_auth_password"] = password
        elif api_key:
            client_options["api_key"] = api_key

        # TODO #1335: Use version-specific support for metrics stores after 7.8.0.
        if self.probe_version:
            distribution_flavor, distribution_version, _, _ = client.cluster_distribution_version(
                hosts=hosts, client_options=client_options
            )
            self._cluster_version = distribution_version

        factory = client_factory(
            hosts=hosts,
            client_options=client_options,
            distribution_version=distribution_version,
            distribution_flavor=distribution_flavor,
        )
        self._client = factory.create()

    def create(self):
        c = EsClient(self._client)
        return c


class IndexTemplateProvider:
    """
    Abstracts how the Rally index template is retrieved. Intended for testing.
    """

    def __init__(self, cfg: types.Config):
        self._config = cfg
        self._number_of_shards = self._config.opts("reporting", "datastore.number_of_shards", default_value=None, mandatory=False)
        self._number_of_replicas = self._config.opts("reporting", "datastore.number_of_replicas", default_value=None, mandatory=False)
        self.script_dir = self._config.opts("node", "rally.root")

    def metrics_template(self):
        return self._read("metrics-template")

    def races_template(self):
        return self._read("races-template")

    def results_template(self):
        return self._read("results-template")

    def annotations_template(self):
        return self._read("annotation-template")

    def _read(self, template_name):
        with open("%s/resources/%s.json" % (self.script_dir, template_name), encoding="utf-8") as f:
            template = json.load(f)
            if self._number_of_shards is not None:
                if int(self._number_of_shards) < 1:
                    raise exceptions.SystemSetupError(
                        f"The setting: datastore.number_of_shards must be >= 1. Please "
                        f"check the configuration in {self._config.config_file.location}"
                    )
                template["template"]["settings"]["index"]["number_of_shards"] = int(self._number_of_shards)
            if self._number_of_replicas is not None:
                template["template"]["settings"]["index"]["number_of_replicas"] = int(self._number_of_replicas)
            return json.dumps(template)


class MetaInfoScope(Enum):
    """
    Defines the scope of a meta-information. Meta-information provides more context for a metric, for example the concrete version
    of Elasticsearch that has been benchmarked or environment information like CPU model or OS.
    """

    cluster = 1
    """
    Cluster level meta-information is valid for all nodes in the cluster (e.g. the benchmarked Elasticsearch version)
    """
    node = 3
    """
    Node level meta-information is valid for a single node (e.g. GC times)
    """


def calculate_results(store, race):
    calc = GlobalStatsCalculator(store, race.track, race.challenge)
    return calc()


def calculate_system_results(store, node_name):
    calc = SystemStatsCalculator(store, node_name)
    return calc()


def metrics_store(cfg: types.Config, read_only=True, track=None, challenge=None, car=None, meta_info=None):
    """
    Creates a proper metrics store based on the current configuration.

    :param cfg: Config object.
    :param read_only: Whether to open the metrics store only for reading (Default: True).
    :return: A metrics store implementation.
    """
    cls = metrics_store_class(cfg)
    store = cls(cfg=cfg, meta_info=meta_info)
    logging.getLogger(__name__).info("Creating %s", str(store))

    race_id = cfg.opts("system", "race.id")
    race_timestamp = cfg.opts("system", "time.start")
    selected_car = cfg.opts("mechanic", "car.names") if car is None else car

    store.open(race_id, race_timestamp, track, challenge, selected_car, create=not read_only)
    return store


def metrics_store_class(cfg: types.Config):
    if cfg.opts("reporting", "datastore.type") == "elasticsearch":
        return EsMetricsStore
    else:
        return InMemoryMetricsStore


class SampleType(IntEnum):
    Warmup = 0
    Normal = 1


class MetricsStore:
    """
    Abstract metrics store
    """

    def __init__(self, cfg: types.Config, clock=time.Clock, meta_info=None):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        self._config = cfg
        self._race_id = None
        self._race_timestamp = None
        self._track = None
        self._track_params = cfg.opts("track", "params", default_value={}, mandatory=False)
        self._challenge = None
        self._car = None
        self._car_name = None
        self._environment_name = cfg.opts("system", "env.name")
        self.opened = False
        if meta_info is None:
            self._meta_info = {}
        else:
            self._meta_info = meta_info
        # ensure mandatory keys are always present
        if MetaInfoScope.cluster not in self._meta_info:
            self._meta_info[MetaInfoScope.cluster] = {}
        if MetaInfoScope.node not in self._meta_info:
            self._meta_info[MetaInfoScope.node] = {}
        self._clock = clock
        self._stop_watch = self._clock.stop_watch()
        self.logger = logging.getLogger(__name__)

    def open(self, race_id=None, race_timestamp=None, track_name=None, challenge_name=None, car_name=None, ctx=None, create=False):
        """
        Opens a metrics store for a specific race, track, challenge and car.

        :param race_id: The race id. This attribute is sufficient to uniquely identify a race.
        :param race_timestamp: The race timestamp as a datetime.
        :param track_name: Track name.
        :param challenge_name: Challenge name.
        :param car_name: Car name.
        :param ctx: An metrics store open context retrieved from another metrics store with ``#open_context``.
        :param create: True if an index should be created (if necessary). This is typically True, when attempting to write metrics and
        False when it is just opened for reading (as we can assume all necessary indices exist at this point).
        """
        if ctx:
            self._race_id = ctx["race-id"]
            self._race_timestamp = ctx["race-timestamp"]
            self._track = ctx["track"]
            self._challenge = ctx["challenge"]
            self._car = ctx["car"]
        else:
            self._race_id = race_id
            self._race_timestamp = time.to_iso8601(race_timestamp)
            self._track = track_name
            self._challenge = challenge_name
            self._car = car_name
        assert self._race_id is not None, "Attempting to open metrics store without a race id"
        assert self._race_timestamp is not None, "Attempting to open metrics store without a race timestamp"

        self._car_name = "+".join(self._car) if isinstance(self._car, list) else self._car

        self.logger.info(
            "Opening metrics store for race timestamp=[%s], track=[%s], challenge=[%s], car=[%s]",
            self._race_timestamp,
            self._track,
            self._challenge,
            self._car,
        )

        user_tags = self._config.opts("race", "user.tags", default_value={}, mandatory=False)
        for k, v in user_tags.items():
            # prefix user tag with "tag_" in order to avoid clashes with our internal meta data
            self.add_meta_info(MetaInfoScope.cluster, None, "tag_%s" % k, v)
        # Don't store it for each metrics record as it's probably sufficient on race level
        # self.add_meta_info(MetaInfoScope.cluster, None, "rally_version", version.version())
        self._stop_watch.start()
        self.opened = True

    def reset_relative_time(self):
        """
        Resets the internal relative-time counter to zero.
        """
        self._stop_watch.start()

    def flush(self, refresh=True):
        """
        Explicitly flushes buffered metrics to the metric store. It is not required to flush before closing the metrics store.
        """
        raise NotImplementedError("abstract method")

    def close(self):
        """
        Closes the metric store. Note that it is mandatory to close the metrics store when it is no longer needed as it only persists
        metrics on close (in order to avoid additional latency during the benchmark).
        """
        self.logger.info("Closing metrics store.")
        self.opened = False
        self.flush()
        self._clear_meta_info()

    def add_meta_info(self, scope, scope_key, key, value):
        """
        Adds new meta information to the metrics store. All metrics entries that are created after calling this method are guaranteed to
        contain the added meta info (provided is on the same level or a level below, e.g. a cluster level metric will not contain node
        level meta information but all cluster level meta information will be contained in a node level metrics record).

        :param scope: The scope of the meta information. See MetaInfoScope.
        :param scope_key: The key within the scope. For cluster level metrics None is expected, for node level metrics the node name.
        :param key: The key of the meta information.
        :param value: The value of the meta information.
        """
        if scope == MetaInfoScope.cluster:
            self._meta_info[MetaInfoScope.cluster][key] = value
        elif scope == MetaInfoScope.node:
            if scope_key not in self._meta_info[MetaInfoScope.node]:
                self._meta_info[MetaInfoScope.node][scope_key] = {}
            self._meta_info[MetaInfoScope.node][scope_key][key] = value
        else:
            raise exceptions.SystemSetupError("Unknown meta info scope [%s]" % scope)

    def _clear_meta_info(self):
        """
        Clears all internally stored meta-info. This is considered Rally internal API and not intended for normal client consumption.
        """
        self._meta_info = {MetaInfoScope.cluster: {}, MetaInfoScope.node: {}}

    @property
    def open_context(self):
        return {
            "race-id": self._race_id,
            "race-timestamp": self._race_timestamp,
            "track": self._track,
            "challenge": self._challenge,
            "car": self._car,
        }

    def put_value_cluster_level(
        self,
        name,
        value,
        unit=None,
        task=None,
        operation=None,
        operation_type=None,
        sample_type=SampleType.Normal,
        absolute_time=None,
        relative_time=None,
        meta_data=None,
    ):
        """
        Adds a new cluster level value metric.

        :param name: The name of the metric.
        :param value: The metric value. It is expected to be numeric.
        :param unit: The unit of this metric value (e.g. ms, docs/s). Optional. Defaults to None.
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type: Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(
            MetaInfoScope.cluster,
            None,
            name,
            value,
            unit,
            task,
            operation,
            operation_type,
            sample_type,
            absolute_time,
            relative_time,
            meta_data,
        )

    def put_value_node_level(
        self,
        node_name,
        name,
        value,
        unit=None,
        task=None,
        operation=None,
        operation_type=None,
        sample_type=SampleType.Normal,
        absolute_time=None,
        relative_time=None,
        meta_data=None,
    ):
        """
        Adds a new node level value metric.

        :param name: The name of the metric.
        :param node_name: The name of the cluster node for which this metric has been determined.
        :param value: The metric value. It is expected to be numeric.
        :param unit: The unit of this metric value (e.g. ms, docs/s). Optional. Defaults to None.
        :param task: The task name to which this value applies. Optional. Defaults to None.
        :param operation: The operation name to which this value applies. Optional. Defaults to None.
        :param operation_type: The operation type to which this value applies. Optional. Defaults to None.
        :param sample_type: Whether this is a warmup or a normal measurement sample. Defaults to SampleType.Normal.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        """
        self._put_metric(
            MetaInfoScope.node,
            node_name,
            name,
            value,
            unit,
            task,
            operation,
            operation_type,
            sample_type,
            absolute_time,
            relative_time,
            meta_data,
        )

    def _put_metric(
        self,
        level,
        level_key,
        name,
        value,
        unit,
        task,
        operation,
        operation_type,
        sample_type,
        absolute_time=None,
        relative_time=None,
        meta_data=None,
    ):
        if level == MetaInfoScope.cluster:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
        elif level == MetaInfoScope.node:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
            if level_key in self._meta_info[MetaInfoScope.node]:
                meta.update(self._meta_info[MetaInfoScope.node][level_key])
        else:
            raise exceptions.SystemSetupError("Unknown meta info level [%s] for metric [%s]" % (level, name))
        if meta_data:
            meta.update(meta_data)

        if absolute_time is None:
            absolute_time = self._clock.now()
        if relative_time is None:
            relative_time = self._stop_watch.split_time()

        doc = {
            "@timestamp": time.to_epoch_millis(absolute_time),
            "relative-time": convert.seconds_to_ms(relative_time),
            "race-id": self._race_id,
            "race-timestamp": self._race_timestamp,
            "environment": self._environment_name,
            "track": self._track,
            "challenge": self._challenge,
            "car": self._car_name,
            "name": name,
            "value": value,
            "unit": unit,
            "sample-type": sample_type.name.lower(),
            "meta": meta,
        }
        if task:
            doc["task"] = task
        if operation:
            doc["operation"] = operation
        if operation_type:
            doc["operation-type"] = operation_type
        if self._track_params:
            doc["track-params"] = self._track_params
        self._add(doc)

    def put_doc(self, doc, level=None, node_name=None, meta_data=None, absolute_time=None, relative_time=None):
        """
        Adds a new document to the metrics store. It will merge additional properties into the doc such as timestamps or track info.

        :param doc: The raw document as a ``dict``. Ownership is transferred to the metrics store (i.e. don't reuse that object).
        :param level: Whether these are cluster or node-level metrics. May be ``None`` if not applicable.
        :param node_name: The name of the node in case metrics are on node level.
        :param meta_data: A dict, containing additional key-value pairs. Defaults to None.
        :param absolute_time: The absolute timestamp in seconds since epoch when this metric record is stored. Defaults to None. The metrics
               store will derive the timestamp automatically.
        :param relative_time: The relative timestamp in seconds since the start of the benchmark when this metric record is stored.
               Defaults to None. The metrics store will derive the timestamp automatically.
        """
        if level == MetaInfoScope.cluster:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
        elif level == MetaInfoScope.node:
            meta = self._meta_info[MetaInfoScope.cluster].copy()
            if node_name in self._meta_info[MetaInfoScope.node]:
                meta.update(self._meta_info[MetaInfoScope.node][node_name])
        elif level is None:
            meta = None
        else:
            raise exceptions.SystemSetupError(f"Unknown meta info level [{level}]")

        if meta and meta_data:
            meta.update(meta_data)

        if absolute_time is None:
            absolute_time = self._clock.now()
        if relative_time is None:
            relative_time = self._stop_watch.split_time()

        doc.update(
            {
                "@timestamp": time.to_epoch_millis(absolute_time),
                "relative-time": convert.seconds_to_ms(relative_time),
                "race-id": self._race_id,
                "race-timestamp": self._race_timestamp,
                "environment": self._environment_name,
                "track": self._track,
                "challenge": self._challenge,
                "car": self._car_name,
            }
        )
        if meta:
            doc["meta"] = meta
        if self._track_params:
            doc["track-params"] = self._track_params

        self._add(doc)

    def bulk_add(self, memento):
        """
        Adds raw metrics store documents previously created with #to_externalizable()

        :param memento: The external representation as returned by #to_externalizable().
        """
        if memento:
            self.logger.debug("Restoring in-memory representation of metrics store.")
            for doc in pickle.loads(zlib.decompress(memento)):
                self._add(doc)

    def to_externalizable(self, clear=False):
        raise NotImplementedError("abstract method")

    def _add(self, doc):
        """
        Adds a new document to the metrics store

        :param doc: The new document.
        """
        raise NotImplementedError("abstract method")

    def get_one(
        self, name, sample_type=None, node_name=None, task=None, mapper=lambda doc: doc["value"], sort_key=None, sort_reverse=False
    ):
        """
        Gets one value for the given metric name (even if there should be more than one).

        :param name: The metric name to query.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param node_name The name of the node where this metric was gathered. Optional.
        :param task The task name to query. Optional.
        :param sort_key The key to sort the docs before returning the first value. Optional.
        :param sort_reverse  The flag to reverse the sort. Optional.
        :return: The corresponding value for the given metric name or None if there is no value.
        """
        raise NotImplementedError("abstract method")

    @staticmethod
    def _first_or_none(values):
        return values[0] if values else None

    def get(self, name, task=None, operation_type=None, sample_type=None, node_name=None):
        """
        Gets all raw values for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param node_name The name of the node where this metric was gathered. Optional.
        :return: A list of all values for the given metric.
        """
        return self._get(name, task, operation_type, sample_type, node_name, lambda doc: doc["value"])

    def get_raw(self, name, task=None, operation_type=None, sample_type=None, node_name=None, mapper=lambda doc: doc):
        """
        Gets all raw records for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param node_name The name of the node where this metric was gathered. Optional.
        :param mapper A record mapper. By default, the complete record is returned.
        :return: A list of all raw records for the given metric.
        """
        return self._get(name, task, operation_type, sample_type, node_name, mapper)

    def get_unit(self, name, task=None, operation_type=None, node_name=None):
        """
        Gets the unit for the given metric name.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param node_name The name of the node where this metric was gathered. Optional.
        :return: The corresponding unit for the given metric name or None if no metric record is available.
        """
        # does not make too much sense to ask for a sample type here
        return self._first_or_none(self._get(name, task, operation_type, None, node_name, lambda doc: doc["unit"]))

    def _get(self, name, task, operation_type, sample_type, node_name, mapper):
        raise NotImplementedError("abstract method")

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        """
        Gets the error rate for a specific task.

        :param task The task name to query.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: A float between 0.0 and 1.0 (inclusive) representing the error rate.
        """
        raise NotImplementedError("abstract method")

    def get_stats(self, name, task=None, operation_type=None, sample_type=None):
        """
        Gets standard statistics for the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: A metric_stats structure.
        """
        raise NotImplementedError("abstract method")

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        """
        Retrieves percentile metrics for the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :param percentiles: An optional list of percentiles to show. If None is provided, by default the 99th, 99.9th and 100th percentile
        are determined. Ensure that there are enough data points in the metrics store (e.g. it makes no sense to retrieve a 99.9999
        percentile when there are only 10 values).
        :return: An ordered dictionary of the determined percentile values in ascending order. Key is the percentile, value is the
        determined value at this percentile. If no percentiles could be determined None is returned.
        """
        raise NotImplementedError("abstract method")

    def get_median(self, name, task=None, operation_type=None, sample_type=None):
        """
        Retrieves median value of the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The median value.
        """
        median = "50.0"
        percentiles = self.get_percentiles(name, task, operation_type, sample_type, percentiles=[median])
        return percentiles[median] if percentiles else None

    def get_mean(self, name, task=None, operation_type=None, sample_type=None):
        """
        Retrieves mean of the given metric.

        :param name: The metric name to query.
        :param task The task name to query. Optional.
        :param operation_type The operation type to query. Optional.
        :param sample_type The sample type to query. Optional. By default, all samples are considered.
        :return: The mean.
        """
        stats = self.get_stats(name, task, operation_type, sample_type)
        return stats["avg"] if stats else None


class EsMetricsStore(MetricsStore):
    """
    A metrics store backed by Elasticsearch.
    """

    def __init__(
        self,
        cfg: types.Config,
        client_factory_class=EsClientFactory,
        index_template_provider_class=IndexTemplateProvider,
        clock=time.Clock,
        meta_info=None,
    ):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        MetricsStore.__init__(self, cfg=cfg, clock=clock, meta_info=meta_info)
        self._index = None
        self._client = client_factory_class(cfg).create()
        self._index_template_provider = index_template_provider_class(cfg)
        self._docs = None

    def open(self, race_id=None, race_timestamp=None, track_name=None, challenge_name=None, car_name=None, ctx=None, create=False):
        self._docs = []
        MetricsStore.open(self, race_id, race_timestamp, track_name, challenge_name, car_name, ctx, create)
        self._index = self.index_name()
        # reduce a bit of noise in the metrics cluster log
        if create:
            self._ensure_index_template()
            if not self._client.exists(index=self._index):
                self._client.create_index(index=self._index)
            else:
                self.logger.info("[%s] already exists.", self._index)
        else:
            # we still need to check for the correct index name - prefer the one with the suffix
            new_name = self._migrated_index_name(self._index)
            if self._client.exists(index=new_name):
                self._index = new_name

        # ensure we can search immediately after opening
        self._client.refresh(index=self._index)

    def _ensure_index_template(self):
        new_template: str = self._get_template()

        old_template: dict | None = None
        if self._client.template_exists("rally-metrics"):
            for t in self._client.get_template("rally-metrics").body.get("index_templates", []):
                old_template = t.get("index_template", {}).get("template", {})
                break

        if old_template is None:
            self.logger.info(
                "Create index template:\n%s",
                pretty.dump(json.loads(new_template).get("template", {}), pretty.Flag.FLAT_DICT),
            )
        else:
            diff = pretty.diff(old_template, json.loads(new_template).get("template", {}), pretty.Flag.FLAT_DICT)
            if diff == "":
                self.logger.debug("Keep existing template (it is identical)")
                return
            if not convert.to_bool(
                self._config.opts(section="reporting", key="datastore.overwrite_existing_templates", default_value=False, mandatory=False)
            ):
                self.logger.debug("Keep existing template (datastore.overwrite_existing_templates = false):\n%s", diff)
                return
            self.logger.warning("Overwrite existing index template (datastore.overwrite_existing_templates = true):\n%s", diff)

        self._client.put_template("rally-metrics", new_template)

    def index_name(self):
        ts = time.from_iso8601(self._race_timestamp)
        return "rally-metrics-%04d-%02d" % (ts.year, ts.month)

    def _migrated_index_name(self, original_name):
        return f"{original_name}.new"

    def _get_template(self):
        return self._index_template_provider.metrics_template()

    def flush(self, refresh=True):
        if self._docs:
            sw = time.StopWatch()
            sw.start()
            self._client.bulk_index(index=self._index, items=self._docs)
            sw.stop()
            self.logger.info(
                "Successfully added %d metrics documents for race timestamp=[%s], track=[%s], challenge=[%s], car=[%s] in [%f] seconds.",
                len(self._docs),
                self._race_timestamp,
                self._track,
                self._challenge,
                self._car,
                sw.total_time(),
            )
        self._docs = []
        # ensure we can search immediately after flushing
        if refresh:
            self._client.refresh(index=self._index)

    def _add(self, doc):
        self._docs.append(doc)

    def _get(self, name, task, operation_type, sample_type, node_name, mapper):
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type, node_name),
            "track_total_hits": True,
            "size": 10000,
        }
        self.logger.debug("Issuing get against index=[%s], query=[%s].", self._index, query)
        result = self._client.search(index=self._index, body=query)
        es_count = result["hits"]["total"]["value"]
        self.logger.debug("Metrics query found [%s] results.", es_count)
        if es_count != len(result["hits"]["hits"]):
            self.logger.warning("Metrics query returned [%d] out of [%s] matching docs.", len(result["hits"]["hits"]), es_count)
        return [mapper(v["_source"]) for v in result["hits"]["hits"]]

    def get_one(
        self, name, sample_type=None, node_name=None, task=None, mapper=lambda doc: doc["value"], sort_key=None, sort_reverse=False
    ):
        order = "desc" if sort_reverse else "asc"
        query = {
            "query": self._query_by_name(name, task, None, sample_type, node_name),
            "size": 1,
        }
        if sort_key:
            query["sort"] = [{sort_key: {"order": order}}]
        self.logger.debug("Issuing get against index=[%s], query=[%s].", self._index, query)
        result = self._client.search(index=self._index, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        self.logger.debug("Metrics query produced [%s] results.", hits)
        if hits > 0:
            return mapper(result["hits"]["hits"][0]["_source"])
        else:
            return None

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        query = {
            "query": self._query_by_name("service_time", task, operation_type, sample_type, None),
            "size": 0,
            "aggs": {
                "error_rate": {
                    "terms": {
                        "field": "meta.success",
                    },
                },
            },
        }
        self.logger.debug("Issuing get_error_rate against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        buckets = result["aggregations"]["error_rate"]["buckets"]
        self.logger.debug("Query returned [%d] buckets.", len(buckets))
        count_success = 0
        count_errors = 0
        for bucket in buckets:
            k = bucket["key_as_string"]
            doc_count = int(bucket["doc_count"])
            self.logger.debug("Processing key [%s] with [%d] docs.", k, doc_count)
            if k == "true":
                count_success = doc_count
            elif k == "false":
                count_errors = doc_count
            else:
                self.logger.warning("Unrecognized bucket key [%s] with [%d] docs.", k, doc_count)

        if count_errors == 0:
            return 0.0
        elif count_success == 0:
            return 1.0
        else:
            return count_errors / (count_errors + count_success)

    def get_stats(self, name, task=None, operation_type=None, sample_type=None):
        """
        Gets standard statistics for the given metric name.

        :return: A metric_stats structure. For details please refer to
        https://www.elastic.co/guide/en/elasticsearch/reference/current/search-aggregations-metrics-stats-aggregation.html
        """
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type, None),
            "size": 0,
            "aggs": {
                "metric_stats": {
                    "stats": {
                        "field": "value",
                    },
                },
            },
        }
        self.logger.debug("Issuing get_stats against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        return result["aggregations"]["metric_stats"]

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        if percentiles is None:
            percentiles = [99, 99.9, 100]
        query = {
            "query": self._query_by_name(name, task, operation_type, sample_type, None),
            "size": 0,
            "aggs": {
                "percentile_stats": {
                    "percentiles": {
                        "field": "value",
                        "percents": percentiles,
                    },
                },
            },
        }
        self.logger.debug("Issuing get_percentiles against index=[%s], query=[%s]", self._index, query)
        result = self._client.search(index=self._index, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        self.logger.debug("get_percentiles produced %d hits", hits)
        if hits > 0:
            raw = result["aggregations"]["percentile_stats"]["values"]
            return collections.OrderedDict(sorted(raw.items(), key=lambda t: float(t[0])))
        else:
            return None

    def _query_by_name(self, name, task, operation_type, sample_type, node_name):
        q = {
            "bool": {
                "filter": [
                    {
                        "term": {
                            "race-id": self._race_id,
                        },
                    },
                    {
                        "term": {
                            "name": name,
                        },
                    },
                ],
            },
        }
        if task:
            q["bool"]["filter"].append(
                {
                    "term": {
                        "task": task,
                    },
                },
            )
        if operation_type:
            q["bool"]["filter"].append(
                {
                    "term": {
                        "operation-type": operation_type,
                    },
                },
            )
        if sample_type:
            q["bool"]["filter"].append(
                {
                    "term": {
                        "sample-type": sample_type.name.lower(),
                    },
                },
            )
        if node_name:
            q["bool"]["filter"].append(
                {
                    "term": {
                        "meta.node_name": node_name,
                    },
                },
            )
        return q

    def to_externalizable(self, clear=False):
        # no need for an externalizable representation - stores everything directly
        return None

    def __str__(self):
        return "Elasticsearch metrics store"


class InMemoryMetricsStore(MetricsStore):
    def __init__(self, cfg: types.Config, clock=time.Clock, meta_info=None):
        """

        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param clock: This parameter is optional and needed for testing.
        :param meta_info: This parameter is optional and intended for creating a metrics store with a previously serialized meta-info.
        """
        super().__init__(cfg=cfg, clock=clock, meta_info=meta_info)
        self.docs = []

    def __del__(self):
        """
        Deletes the metrics store instance.
        """
        del self.docs

    def _add(self, doc):
        self.docs.append(doc)

    def flush(self, refresh=True):
        pass

    def to_externalizable(self, clear=False):
        docs = self.docs
        if clear:
            self.docs = []
        compressed = zlib.compress(pickle.dumps(docs))
        self.logger.debug(
            "Compression changed size of metric store from [%d] bytes to [%d] bytes", sys.getsizeof(docs, -1), sys.getsizeof(compressed, -1)
        )
        return compressed

    def get_percentiles(self, name, task=None, operation_type=None, sample_type=None, percentiles=None):
        if percentiles is None:
            percentiles = [99, 99.9, 100]
        result = collections.OrderedDict()
        values = self.get(name, task, operation_type, sample_type)
        if len(values) > 0:
            sorted_values = sorted(values)
            for percentile in percentiles:
                result[percentile] = self.percentile_value(sorted_values, percentile)
        return result

    @staticmethod
    def percentile_value(sorted_values, percentile):
        """
        Calculates a percentile value for a given list of values and a percentile.

        The implementation is based on http://onlinestatbook.com/2/introduction/percentiles.html

        :param sorted_values: A sorted list of raw values for which a percentile should be calculated.
        :param percentile: A percentile between [0, 100]
        :return: the corresponding percentile value.
        """
        rank = float(percentile) / 100.0 * (len(sorted_values) - 1)
        if rank == int(rank):
            return sorted_values[int(rank)]
        else:
            lr = math.floor(rank)
            lr_next = math.ceil(rank)
            fr = rank - lr
            lower_score = sorted_values[lr]
            higher_score = sorted_values[lr_next]
            return lower_score + (higher_score - lower_score) * fr

    def get_error_rate(self, task, operation_type=None, sample_type=None):
        error = 0
        total_count = 0
        for doc in self.docs:
            # we can use any request metrics record (i.e. service time or latency)
            if (
                doc["name"] == "service_time"
                and doc["task"] == task
                and (operation_type is None or doc["operation-type"] == operation_type)
                and (sample_type is None or doc["sample-type"] == sample_type.name.lower())
            ):
                total_count += 1
                if doc["meta"]["success"] is False:
                    error += 1
        if total_count > 0:
            return error / total_count
        else:
            return 0.0

    def get_stats(self, name, task=None, operation_type=None, sample_type=SampleType.Normal):
        values = self.get(name, task, operation_type, sample_type)
        sorted_values = sorted(values)
        if len(sorted_values) > 0:
            return {
                "count": len(sorted_values),
                "min": sorted_values[0],
                "max": sorted_values[-1],
                "avg": statistics.mean(sorted_values),
                "sum": sum(sorted_values),
            }
        else:
            return None

    def _get(self, name, task, operation_type, sample_type, node_name, mapper):
        return [
            mapper(doc)
            for doc in self.docs
            if doc["name"] == name
            and (task is None or doc["task"] == task)
            and (operation_type is None or doc["operation-type"] == operation_type)
            and (sample_type is None or doc["sample-type"] == sample_type.name.lower())
            and (node_name is None or doc.get("meta", {}).get("node_name") == node_name)
        ]

    def get_one(
        self, name, sample_type=None, node_name=None, task=None, mapper=lambda doc: doc["value"], sort_key=None, sort_reverse=False
    ):
        if sort_key:
            docs = sorted(self.docs, key=lambda k: k[sort_key], reverse=sort_reverse)
        else:
            docs = self.docs
        for doc in docs:
            if (
                doc["name"] == name
                and (task is None or doc["task"] == task)
                and (sample_type is None or doc["sample-type"] == sample_type.name.lower())
                and (node_name is None or doc.get("meta", {}).get("node_name") == node_name)
            ):
                return mapper(doc)
        return None

    def __str__(self):
        return "in-memory metrics store"


def race_store(cfg: types.Config):
    """
    Creates a proper race store based on the current configuration.
    :param cfg: Config object. Mandatory.
    :return: A race store implementation.
    """
    logger = logging.getLogger(__name__)
    if cfg.opts("reporting", "datastore.type") == "elasticsearch":
        logger.info("Creating ES race store")
        return CompositeRaceStore(EsRaceStore(cfg), FileRaceStore(cfg))
    else:
        logger.info("Creating file race store")
        return FileRaceStore(cfg)


def results_store(cfg: types.Config):
    """
    Creates a proper race store based on the current configuration.
    :param cfg: Config object. Mandatory.
    :return: A race store implementation.
    """
    logger = logging.getLogger(__name__)
    if cfg.opts("reporting", "datastore.type") == "elasticsearch":
        logger.info("Creating ES results store")
        return EsResultsStore(cfg)
    else:
        logger.info("Creating no-op results store")
        return NoopResultsStore()


def delete_race(cfg: types.Config):
    race_store(cfg).delete_race()


def delete_annotation(cfg: types.Config):
    race_store(cfg).delete_annotation()


def list_annotations(cfg: types.Config):
    race_store(cfg).list_annotations()


def add_annotation(cfg: types.Config):
    race_store(cfg).add_annotation()


def list_races(cfg: types.Config):
    def format_dict(d):
        if d:
            items = sorted(d.items())
            return ", ".join(["%s=%s" % (k, v) for k, v in items])
        else:
            return None

    output_format: str = cfg.opts("system", "list.races.format")
    if output_format == "text":
        races = []
        for race in race_store(cfg).list():
            races.append(
                [
                    race.race_id,
                    time.to_iso8601(race.race_timestamp),
                    race.track,
                    race.challenge_name,
                    race.car_name,
                    race.distribution_version,
                    race.revision,
                    race.rally_version,
                    race.track_revision,
                    race.team_revision,
                    format_dict(race.user_tags),
                ]
            )

        if len(races) > 0:
            console.println("\nRecent races:\n")
            console.println(
                tabulate.tabulate(
                    races,
                    headers=[
                        "Race ID",
                        "Race Timestamp",
                        "Track",
                        "Challenge",
                        "Car",
                        "ES Version",
                        "Revision",
                        "Rally Version",
                        "Track Revision",
                        "Team Revision",
                        "User Tags",
                    ],
                )
            )
        else:
            console.println("")
            console.println("No recent races found.")

    elif output_format == "json":
        d = {"races": [race.as_dict() for race in race_store(cfg).list()]}
        console.println(json.dumps(d, indent=2))

    else:
        raise exceptions.RallyAssertionError(f"Unknown output format [{output_format}]")


def create_race(cfg: types.Config, track, challenge, track_revision=None):
    car = cfg.opts("mechanic", "car.names")
    environment = cfg.opts("system", "env.name")
    race_id = cfg.opts("system", "race.id")
    race_timestamp = cfg.opts("system", "time.start")
    user_tags = cfg.opts("race", "user.tags", default_value={}, mandatory=False)
    pipeline = cfg.opts("race", "pipeline")
    track_params = cfg.opts("track", "params")
    car_params = cfg.opts("mechanic", "car.params")
    plugin_params = cfg.opts("mechanic", "plugin.params")
    rally_version = version.version()
    rally_revision = version.revision()

    return Race(
        rally_version,
        rally_revision,
        environment,
        race_id,
        race_timestamp,
        pipeline,
        user_tags,
        track,
        track_params,
        challenge,
        car,
        car_params,
        plugin_params,
        track_revision,
    )


class Race:
    def __init__(
        self,
        rally_version,
        rally_revision,
        environment_name,
        race_id,
        race_timestamp,
        pipeline,
        user_tags,
        track,
        track_params,
        challenge,
        car,
        car_params,
        plugin_params,
        track_revision=None,
        team_revision=None,
        distribution_version=None,
        distribution_flavor=None,
        revision=None,
        results=None,
        meta_data=None,
    ):
        if results is None:
            results = {}
        # this happens when the race is created initially
        if meta_data is None:
            meta_data = {}
            if track:
                meta_data.update(track.meta_data)
            if challenge:
                meta_data.update(challenge.meta_data)
        self.rally_version = rally_version
        self.rally_revision = rally_revision
        self.environment_name = environment_name
        self.race_id = race_id
        self.race_timestamp = race_timestamp
        self.pipeline = pipeline
        self.user_tags = user_tags
        self.track = track
        self.track_params = track_params
        self.challenge = challenge
        self.car = car
        self.car_params = car_params
        self.plugin_params = plugin_params
        self.track_revision = track_revision
        self.team_revision = team_revision
        self.distribution_version = distribution_version
        self.distribution_flavor = distribution_flavor
        self.revision = revision
        self.results = results
        self.meta_data = meta_data

    @property
    def track_name(self):
        return str(self.track)

    @property
    def challenge_name(self):
        return str(self.challenge) if self.challenge else None

    @property
    def car_name(self):
        return "+".join(self.car) if isinstance(self.car, list) else self.car

    def add_results(self, results):
        self.results = results

    def as_dict(self):
        """
        :return: A dict representation suitable for persisting this race instance as JSON.
        """
        d = {
            "rally-version": self.rally_version,
            "rally-revision": self.rally_revision,
            "environment": self.environment_name,
            "race-id": self.race_id,
            "race-timestamp": time.to_iso8601(self.race_timestamp),
            "pipeline": self.pipeline,
            "user-tags": self.user_tags,
            "track": self.track_name,
            "car": self.car,
            "cluster": {
                "revision": self.revision,
                "distribution-version": self.distribution_version,
                "distribution-flavor": self.distribution_flavor,
                "team-revision": self.team_revision,
            },
        }
        if self.results:
            if hasattr(self.results, "as_dict"):
                d["results"] = self.results.as_dict()
            else:
                d["results"] = self.results
        if self.track_revision:
            d["track-revision"] = self.track_revision
        if self.challenge:
            if not hasattr(self.challenge, "auto_generated") or not self.challenge.auto_generated:
                d["challenge"] = self.challenge_name
        if self.track_params:
            d["track-params"] = self.track_params
        if self.car_params:
            d["car-params"] = self.car_params
        if self.plugin_params:
            d["plugin-params"] = self.plugin_params
        return d

    def to_result_dicts(self):
        """
        :return: a list of dicts, suitable for persisting the results of this race in a format that is Kibana-friendly.
        """
        result_template = {
            "rally-version": self.rally_version,
            "rally-revision": self.rally_revision,
            "environment": self.environment_name,
            "race-id": self.race_id,
            "race-timestamp": time.to_iso8601(self.race_timestamp),
            "distribution-version": self.distribution_version,
            "distribution-flavor": self.distribution_flavor,
            "user-tags": self.user_tags,
            "track": self.track_name,
            "challenge": self.challenge_name,
            "car": self.car_name,
            # allow to logically delete records, e.g. for UI purposes when we only want to show the latest result
            "active": True,
        }
        if versions.is_version_identifier(self.distribution_version):
            result_template["distribution-major-version"] = versions.major_version(self.distribution_version)
        if self.team_revision:
            result_template["team-revision"] = self.team_revision
        if self.track_revision:
            result_template["track-revision"] = self.track_revision
        if self.track_params:
            result_template["track-params"] = self.track_params
        if self.car_params:
            result_template["car-params"] = self.car_params
        if self.plugin_params:
            result_template["plugin-params"] = self.plugin_params
        if self.meta_data:
            result_template["meta"] = self.meta_data

        all_results = []

        for item in self.results.as_flat_list():
            result = result_template.copy()
            result.update(item)
            all_results.append(result)

        return all_results

    @classmethod
    def from_dict(cls, d):
        user_tags = d.get("user-tags", {})
        # TODO: cluster is optional for BWC. This can be removed after some grace period.
        cluster = d.get("cluster", {})
        return Race(
            d["rally-version"],
            d.get("rally-revision"),
            d["environment"],
            d["race-id"],
            time.from_iso8601(d["race-timestamp"]),
            d["pipeline"],
            user_tags,
            d["track"],
            d.get("track-params"),
            d.get("challenge"),
            d["car"],
            d.get("car-params"),
            d.get("plugin-params"),
            track_revision=d.get("track-revision"),
            team_revision=cluster.get("team-revision"),
            distribution_version=cluster.get("distribution-version"),
            distribution_flavor=cluster.get("distribution-flavor"),
            revision=cluster.get("revision"),
            results=d.get("results"),
            meta_data=d.get("meta", {}),
        )


class RaceStore:
    def __init__(self, cfg: types.Config):
        self.cfg = cfg
        self.environment_name = cfg.opts("system", "env.name")

    def find_by_race_id(self, race_id):
        raise NotImplementedError("abstract method")

    def list(self):
        raise NotImplementedError("abstract method")

    def delete_race(self):
        raise NotImplementedError("abstract method")

    def delete_annotation(self):
        raise NotImplementedError("abstract method")

    def list_annotations(self):
        raise NotImplementedError("abstract method")

    def add_annotation(self):
        raise NotImplementedError("abstract method")

    def store_race(self, race):
        raise NotImplementedError("abstract method")

    def _max_results(self):
        return int(self.cfg.opts("system", "list.max_results"))

    def _track(self):
        return self.cfg.opts("system", "admin.track", mandatory=False)

    def _benchmark_name(self):
        return self.cfg.opts("system", "list.races.benchmark_name", mandatory=False)

    def _user_tags(self) -> dict:
        return self.cfg.opts("system", "list.races.user_tags", default_value={}, mandatory=False)

    def _race_timestamp(self):
        return self.cfg.opts("system", "add.race_timestamp")

    def _message(self):
        return self.cfg.opts("system", "add.message")

    def _chart_type(self):
        return self.cfg.opts("system", "add.chart_type", mandatory=False)

    def _chart_name(self):
        return self.cfg.opts("system", "add.chart_name", mandatory=False)

    def _from_date(self):
        return self.cfg.opts("system", "list.from_date", mandatory=False)

    def _to_date(self):
        return self.cfg.opts("system", "list.to_date", mandatory=False)

    def _dry_run(self):
        return self.cfg.opts("system", "admin.dry_run", mandatory=False)

    def _id(self):
        return self.cfg.opts("system", "delete.id")

    def _challenge(self):
        return self.cfg.opts("system", "list.challenge", mandatory=False)


# Does not inherit from RaceStore as it is only a delegator with the same API.
class CompositeRaceStore:
    """
    Internal helper class to store races as file and to Elasticsearch in case users want Elasticsearch as a race store.

    It provides the same API as RaceStore. It delegates writes to all stores and all read operations only the Elasticsearch race store.
    """

    def __init__(self, es_store, file_store):
        self.es_store = es_store
        self.file_store = file_store

    def find_by_race_id(self, race_id):
        return self.es_store.find_by_race_id(race_id)

    def store_race(self, race):
        self.file_store.store_race(race)
        self.es_store.store_race(race)

    def delete_race(self):
        return self.es_store.delete_race()

    def delete_annotation(self):
        return self.es_store.delete_annotation()

    def list_annotations(self):
        return self.es_store.list_annotations()

    def add_annotation(self):
        return self.es_store.add_annotation()

    def list(self):
        return self.es_store.list()


class FileRaceStore(RaceStore):
    def store_race(self, race):
        doc = race.as_dict()
        race_path = paths.race_root(self.cfg, race_id=race.race_id)
        io.ensure_dir(race_path)
        with open(self._race_file(), mode="w", encoding="utf-8") as f:
            f.write(json.dumps(doc, indent=True, ensure_ascii=False))

    def _race_file(self, race_id=None):
        return os.path.join(paths.race_root(cfg=self.cfg, race_id=race_id), "race.json")

    def delete_race(self):
        raise NotImplementedError("Not supported for in-memory datastore.")

    def delete_annotation(self):
        raise NotImplementedError("Not supported for in-memory datastore.")

    def list_annotations(self):
        raise NotImplementedError("Not supported for in-memory datastore.")

    def add_annotation(self):
        raise NotImplementedError("Not supported for in-memory datastore.")

    def list(self):
        results = glob.glob(self._race_file(race_id="*"))
        all_races = self._to_races(results)
        return all_races[: self._max_results()]

    def find_by_race_id(self, race_id):
        race_file = self._race_file(race_id=race_id)
        if io.exists(race_file):
            races = self._to_races([race_file])
            if races:
                return races[0]
        raise exceptions.NotFound(f"No race with race id [{race_id}]")

    def _to_races(self, results):
        races = []
        track = self._track()
        name = self._benchmark_name()
        pattern = "%Y%m%d"
        from_date = self._from_date()
        to_date = self._to_date()
        challenge = self._challenge()
        user_tags = self._user_tags()

        for result in results:
            # noinspection PyBroadException
            try:
                with open(result, encoding="utf-8") as f:
                    races.append(Race.from_dict(json.loads(f.read())))
            except BaseException:
                logging.getLogger(__name__).exception("Could not load race file [%s] (incompatible format?) Skipping...", result)

        if track:
            races = filter(lambda r: r.track == track, races)
        if name:
            filtered_on_name = filter(lambda r: r.user_tags.get("name") == name, races)
            filtered_on_benchmark_name = filter(lambda r: r.user_tags.get("benchmark-name") == name, races)
            races = list(filtered_on_name) + list(filtered_on_benchmark_name)
        if from_date:
            races = filter(lambda r: r.race_timestamp.date() >= datetime.datetime.strptime(from_date, pattern).date(), races)
        if to_date:
            races = filter(lambda r: r.race_timestamp.date() <= datetime.datetime.strptime(to_date, pattern).date(), races)
        if challenge:
            races = filter(lambda r: r.challenge == challenge, races)
        if user_tags:
            races = filter(lambda r: all(r.user_tags.get(k) == v for k, v in user_tags.items()), races)

        return sorted(races, key=lambda r: r.race_timestamp, reverse=True)


class EsRaceStore(RaceStore):
    INDEX_PREFIX = "rally-races-"

    def __init__(self, cfg: types.Config, client_factory_class=EsClientFactory, index_template_provider_class=IndexTemplateProvider):
        """
        Creates a new metrics store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        """
        super().__init__(cfg)
        self.client = client_factory_class(cfg).create()
        self.index_template_provider = index_template_provider_class(cfg)

    def store_race(self, race):
        doc = race.as_dict()
        # always update the mapping to the latest version
        self.client.put_template("rally-races", self.index_template_provider.races_template())
        self.client.index(index=self.index_name(race), item=doc, id=race.race_id)

    def index_name(self, race):
        race_timestamp = race.race_timestamp
        return f"{EsRaceStore.INDEX_PREFIX}{race_timestamp:%Y-%m}"

    def add_annotation(self):
        def _at_midnight(race_timestamp):
            TIMESTAMP_FMT = "%Y%m%dT%H%M%SZ"
            date = datetime.datetime.strptime(race_timestamp, TIMESTAMP_FMT)
            date = date.replace(hour=0, minute=0, second=0, tzinfo=datetime.timezone.utc)
            return date.strftime(TIMESTAMP_FMT)

        environment = self.environment_name
        # To line up annotations with chart data points, use midnight of day N as this is
        # what the chart use too.
        race_timestamp = _at_midnight(self._race_timestamp())
        track = self._track()
        chart_type = self._chart_type()
        chart_name = self._chart_name()
        message = self._message()
        annotation_id = str(uuid.uuid4())
        dry_run = self._dry_run()

        if dry_run:
            console.println(
                f"Would add annotation with message [{message}] for environment=[{environment}], race timestamp=[{race_timestamp}], "
                f"track=[{track}], chart type=[{chart_type}], chart name=[{chart_name}]"
            )
        else:
            if not self.client.exists(index="rally-annotations"):
                # create or overwrite template on index creation
                self.client.put_template("rally-annotations", self.index_template_provider.annotations_template())
                self.client.create_index(index="rally-annotations")
            self.client.index(
                index="rally-annotations",
                id=annotation_id,
                item={
                    "environment": environment,
                    "race-timestamp": race_timestamp,
                    "track": track,
                    "chart": chart_type,
                    "chart-name": chart_name,
                    "message": message,
                },
            )
            console.println(f"Successfully added annotation [{annotation_id}].")

    def list_annotations(self):
        environment = self.environment_name
        track = self._track()
        from_date = self._from_date()
        to_date = self._to_date()
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {"term": {"environment": environment}},
                        {"range": {"race-timestamp": {"gte": from_date, "lte": to_date, "format": "basic_date"}}},
                    ]
                }
            }
        }
        if track:
            query["query"]["bool"]["filter"].append({"term": {"track": track}})

        query["sort"] = [{"race-timestamp": "desc"}, {"track": "asc"}, {"chart": "asc"}]
        query["size"] = self._max_results()

        result = self.client.search(index="rally-annotations", body=query)
        annotations = []
        hits = result["hits"]["total"]
        if hits == 0:
            console.println(f"No annotations found in environment [{environment}].")
        else:
            for hit in result["hits"]["hits"]:
                src = hit["_source"]
                annotations.append(
                    [
                        hit["_id"],
                        src["race-timestamp"],
                        src.get("track", ""),
                        src.get("chart", ""),
                        src.get("chart-name", ""),
                        src["message"],
                    ]
                )

            if annotations:
                console.println("\nAnnotations:\n")
                console.println(
                    tabulate.tabulate(
                        annotations,
                        headers=["Annotation Id", "Timestamp", "Track", "Chart Type", "Chart Name", "Message"],
                    )
                )

    def delete_annotation(self):
        annotations = self._id().split(",")
        environment = self.environment_name
        if self._dry_run():
            if len(annotations) == 1:
                console.println(f"Would delete annotation with id [{annotations[0]}] in environment [{environment}].")
            else:
                console.println(f"Would delete {len(annotations)} annotations: {annotations} in environment [{environment}].")
        else:
            for annotation_id in annotations:
                result = self.client.delete(index="rally-annotations", id=annotation_id)
                if result["result"] == "deleted":
                    console.println(f"Successfully deleted [{annotation_id}].")
                else:
                    console.println(f"Did not find [{annotation_id}] in environment [{environment}].")

    def delete_race(self):
        races = self._id().split(",")
        environment = self.environment_name
        if self._dry_run():
            if len(races) == 1:
                console.println(f"Would delete race with id {races[0]} in environment {environment}.")
            else:
                console.println(f"Would delete {len(races)} races: {races} in environment {environment}.")
        else:
            for race_id in races:
                selector = {"query": {"bool": {"filter": [{"term": {"environment": environment}}, {"term": {"race-id": race_id}}]}}}
                self.client.delete_by_query(index="rally-races-*", body=selector)
                self.client.delete_by_query(index="rally-metrics-*", body=selector)
                result = self.client.delete_by_query(index="rally-results-*", body=selector)
                if result["deleted"] > 0:
                    console.println(f"Successfully deleted [{race_id}] in environment [{environment}].")
                else:
                    console.println(f"Did not find [{race_id}] in environment [{environment}].")

    def list(self):
        track = self._track()
        name = self._benchmark_name()
        from_date = self._from_date()
        to_date = self._to_date()
        challenge = self._challenge()
        user_tags = self._user_tags()

        filters = [
            {
                "term": {
                    "environment": self.environment_name,
                },
            },
            {"range": {"race-timestamp": {"gte": from_date, "lte": to_date, "format": "basic_date"}}},
        ]

        query = {
            "query": {
                "bool": {
                    "filter": filters,
                },
            },
            "size": self._max_results(),
            "sort": [
                {
                    "race-timestamp": {
                        "order": "desc",
                    },
                },
            ],
        }
        if track:
            query["query"]["bool"]["filter"].append({"term": {"track": track}})
        if name:
            query["query"]["bool"]["filter"].append(
                {"bool": {"should": [{"term": {"user-tags.benchmark-name": name}}, {"term": {"user-tags.name": name}}]}}
            )
        if challenge:
            query["query"]["bool"]["filter"].append({"term": {"challenge": challenge}})
        if user_tags:
            query["query"]["bool"]["filter"].extend([{"term": {f"user-tags.{k}": v}} for k, v in user_tags.items()])
        result = self.client.search(index="%s*" % EsRaceStore.INDEX_PREFIX, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        if hits > 0:
            return [Race.from_dict(v["_source"]) for v in result["hits"]["hits"]]
        else:
            return []

    def find_by_race_id(self, race_id):
        query = {
            "query": {
                "bool": {
                    "filter": [
                        {
                            "term": {
                                "race-id": race_id,
                            },
                        },
                    ],
                },
            },
        }
        result = self.client.search(index="%s*" % EsRaceStore.INDEX_PREFIX, body=query)
        hits = result["hits"]["total"]
        # Elasticsearch 7.0+
        if isinstance(hits, dict):
            hits = hits["value"]
        if hits == 1:
            return Race.from_dict(result["hits"]["hits"][0]["_source"])
        elif hits > 1:
            raise exceptions.RallyAssertionError(f"Expected exactly one race to match race id [{race_id}] but there were [{hits}] matches.")
        else:
            raise exceptions.NotFound(f"No race with race id [{race_id}]")


class EsResultsStore:
    """
    Stores the results of a race in a format that is better suited for reporting with Kibana.
    """

    INDEX_PREFIX = "rally-results-"

    def __init__(self, cfg: types.Config, client_factory_class=EsClientFactory, index_template_provider_class=IndexTemplateProvider):
        """
        Creates a new results store.

        :param cfg: The config object. Mandatory.
        :param client_factory_class: This parameter is optional and needed for testing.
        :param index_template_provider_class: This parameter is optional and needed for testing.
        """
        self.cfg = cfg
        self.client = client_factory_class(cfg).create()
        self.index_template_provider = index_template_provider_class(cfg)

    def store_results(self, race):
        # always update the mapping to the latest version
        self.client.put_template("rally-results", self.index_template_provider.results_template())
        self.client.bulk_index(index=self.index_name(race), items=race.to_result_dicts())

    def index_name(self, race):
        race_timestamp = race.race_timestamp
        return f"{EsResultsStore.INDEX_PREFIX}{race_timestamp:%Y-%m}"


class NoopResultsStore:
    """
    Does not store any results separately as these are stored as part of the race on the file system.
    """

    def store_results(self, race):
        pass


# helper function for encoding and decoding float keys so that the Elasticsearch metrics store can save them.
def encode_float_key(k):
    # ensure that the key is indeed a float to unify the representation (e.g. 50 should be represented as "50_0")
    return str(float(k)).replace(".", "_")


def percentiles_for_sample_size(sample_size):
    # if needed we can come up with something smarter but it'll do for now
    if sample_size < 1:
        raise AssertionError("Percentiles require at least one sample")

    if sample_size == 1:
        return [100]
    elif 1 < sample_size < 10:
        return [50, 100]
    elif 10 <= sample_size < 100:
        return [50, 90, 100]
    elif 100 <= sample_size < 1000:
        return [50, 90, 99, 100]
    elif 1000 <= sample_size < 10000:
        return [50, 90, 99, 99.9, 100]
    else:
        return [50, 90, 99, 99.9, 99.99, 100]


class GlobalStatsCalculator:
    def __init__(self, store, track, challenge):
        self.store = store
        self.logger = logging.getLogger(__name__)
        self.track = track
        self.challenge = challenge

    def __call__(self):
        result = GlobalStats()

        for tasks in self.challenge.schedule:
            for task in tasks:
                t = task.name
                op_type = task.operation.type
                error_rate = self.error_rate(t, op_type)
                duration = self.duration(t)
                if task.operation.include_in_reporting or error_rate > 0:
                    self.logger.debug("Gathering request metrics for [%s].", t)
                    result.add_op_metrics(
                        t,
                        task.operation.name,
                        self.summary_stats("throughput", t, op_type),
                        self.single_latency(t, op_type),
                        self.single_latency(t, op_type, metric_name="service_time"),
                        self.single_latency(t, op_type, metric_name="processing_time"),
                        error_rate,
                        duration,
                        self.merge(self.track.meta_data, self.challenge.meta_data, task.operation.meta_data, task.meta_data),
                    )
        self.logger.debug("Gathering indexing metrics.")
        result.total_time = self.sum("indexing_total_time")
        result.total_time_per_shard = self.shard_stats("indexing_total_time")
        result.indexing_throttle_time = self.sum("indexing_throttle_time")
        result.indexing_throttle_time_per_shard = self.shard_stats("indexing_throttle_time")
        result.merge_time = self.sum("merges_total_time")
        result.merge_time_per_shard = self.shard_stats("merges_total_time")
        result.merge_count = self.sum("merges_total_count")
        result.refresh_time = self.sum("refresh_total_time")
        result.refresh_time_per_shard = self.shard_stats("refresh_total_time")
        result.refresh_count = self.sum("refresh_total_count")
        result.flush_time = self.sum("flush_total_time")
        result.flush_time_per_shard = self.shard_stats("flush_total_time")
        result.flush_count = self.sum("flush_total_count")
        result.merge_throttle_time = self.sum("merges_total_throttled_time")
        result.merge_throttle_time_per_shard = self.shard_stats("merges_total_throttled_time")

        self.logger.debug("Gathering ML max processing times.")
        result.ml_processing_time = self.ml_processing_time_stats()

        self.logger.debug("Gathering garbage collection metrics.")
        result.young_gc_time = self.sum("node_total_young_gen_gc_time")
        result.young_gc_count = self.sum("node_total_young_gen_gc_count")
        result.old_gc_time = self.sum("node_total_old_gen_gc_time")
        result.old_gc_count = self.sum("node_total_old_gen_gc_count")
        result.zgc_cycles_gc_time = self.sum("node_total_zgc_cycles_gc_time")
        result.zgc_cycles_gc_count = self.sum("node_total_zgc_cycles_gc_count")
        result.zgc_pauses_gc_time = self.sum("node_total_zgc_pauses_gc_time")
        result.zgc_pauses_gc_count = self.sum("node_total_zgc_pauses_gc_count")

        self.logger.debug("Gathering segment memory metrics.")
        result.memory_segments = self.median("segments_memory_in_bytes")
        result.memory_doc_values = self.median("segments_doc_values_memory_in_bytes")
        result.memory_terms = self.median("segments_terms_memory_in_bytes")
        result.memory_norms = self.median("segments_norms_memory_in_bytes")
        result.memory_points = self.median("segments_points_memory_in_bytes")
        result.memory_stored_fields = self.median("segments_stored_fields_memory_in_bytes")
        result.dataset_size = self.sum("dataset_size_in_bytes")
        result.store_size = self.sum("store_size_in_bytes")
        result.translog_size = self.sum("translog_size_in_bytes")

        # convert to int, fraction counts are senseless
        median_segment_count = self.median("segments_count")
        result.segment_count = int(median_segment_count) if median_segment_count is not None else median_segment_count

        self.logger.debug("Gathering transform processing times.")
        result.total_transform_processing_times = self.total_transform_metric("total_transform_processing_time")
        result.total_transform_index_times = self.total_transform_metric("total_transform_index_time")
        result.total_transform_search_times = self.total_transform_metric("total_transform_search_time")
        result.total_transform_throughput = self.total_transform_metric("total_transform_throughput")

        self.logger.debug("Gathering Ingest Pipeline metrics.")
        result.ingest_pipeline_cluster_count = self.sum("ingest_pipeline_cluster_count")
        result.ingest_pipeline_cluster_time = self.sum("ingest_pipeline_cluster_time")
        result.ingest_pipeline_cluster_failed = self.sum("ingest_pipeline_cluster_failed")

        self.logger.debug("Gathering disk usage metrics.")
        result.disk_usage_total = self.disk_usage("disk_usage_total")
        result.disk_usage_inverted_index = self.disk_usage("disk_usage_inverted_index")
        result.disk_usage_stored_fields = self.disk_usage("disk_usage_stored_fields")
        result.disk_usage_doc_values = self.disk_usage("disk_usage_doc_values")
        result.disk_usage_points = self.disk_usage("disk_usage_points")
        result.disk_usage_norms = self.disk_usage("disk_usage_norms")
        result.disk_usage_term_vectors = self.disk_usage("disk_usage_term_vectors")

        return result

    def merge(self, *args):
        # This is similar to dict(collections.ChainMap(args)) except that we skip `None` in our implementation.
        result = {}
        for arg in args:
            if arg is not None:
                result.update(arg)
        return result

    def sum(self, metric_name):
        values = self.store.get(metric_name)
        if values:
            return sum(values)
        else:
            return None

    def one(self, metric_name):
        return self.store.get_one(metric_name)

    def summary_stats(self, metric_name, task_name, operation_type):
        mean = self.store.get_mean(metric_name, task=task_name, operation_type=operation_type, sample_type=SampleType.Normal)
        median = self.store.get_median(metric_name, task=task_name, operation_type=operation_type, sample_type=SampleType.Normal)
        unit = self.store.get_unit(metric_name, task=task_name, operation_type=operation_type)
        stats = self.store.get_stats(metric_name, task=task_name, operation_type=operation_type, sample_type=SampleType.Normal)
        if mean and median and stats:
            return {
                "min": stats["min"],
                "mean": mean,
                "median": median,
                "max": stats["max"],
                "unit": unit,
            }
        else:
            return {
                "min": None,
                "mean": None,
                "median": None,
                "max": None,
                "unit": unit,
            }

    def shard_stats(self, metric_name):
        values = self.store.get_raw(metric_name, mapper=lambda doc: doc["per-shard"])
        unit = self.store.get_unit(metric_name)
        if values:
            flat_values = [w for v in values for w in v]
            return {
                "min": min(flat_values),
                "median": statistics.median(flat_values),
                "max": max(flat_values),
                "unit": unit,
            }
        else:
            return {}

    def ml_processing_time_stats(self):
        values = self.store.get_raw("ml_processing_time")
        result = []
        if values:
            for v in values:
                result.append(
                    {"job": v["job"], "min": v["min"], "mean": v["mean"], "median": v["median"], "max": v["max"], "unit": v["unit"]}
                )
        return result

    def total_transform_metric(self, metric_name):
        values = self.store.get_raw(metric_name)
        result = []
        if values:
            for v in values:
                transform_id = v.get("meta", {}).get("transform_id")
                if transform_id is not None:
                    result.append({"id": transform_id, "mean": v["value"], "unit": v["unit"]})
        return result

    def disk_usage(self, metric_name):
        values = self.store.get_raw(metric_name)
        result = []
        if values:
            for v in values:
                meta = v.get("meta", {})
                index = meta.get("index")
                field = meta.get("field")
                if index is not None and field is not None:
                    result.append({"index": index, "field": field, "value": v["value"], "unit": v["unit"]})
        return result

    def error_rate(self, task_name, operation_type):
        return self.store.get_error_rate(task=task_name, operation_type=operation_type, sample_type=SampleType.Normal)

    def duration(self, task_name):
        return self.store.get_one(
            "service_time", task=task_name, mapper=lambda doc: doc["relative-time"], sort_key="relative-time", sort_reverse=True
        )

    def median(self, metric_name, task_name=None, operation_type=None, sample_type=None):
        return self.store.get_median(metric_name, task=task_name, operation_type=operation_type, sample_type=sample_type)

    def single_latency(self, task, operation_type, metric_name="latency"):
        sample_type = SampleType.Normal
        stats = self.store.get_stats(metric_name, task=task, operation_type=operation_type, sample_type=sample_type)
        sample_size = stats["count"] if stats else 0
        if sample_size > 0:
            percentiles = self.store.get_percentiles(
                metric_name,
                task=task,
                operation_type=operation_type,
                sample_type=sample_type,
                percentiles=percentiles_for_sample_size(sample_size),
            )
            mean = self.store.get_mean(metric_name, task=task, operation_type=operation_type, sample_type=sample_type)
            unit = self.store.get_unit(metric_name, task=task, operation_type=operation_type)
            stats = collections.OrderedDict()
            for k, v in percentiles.items():
                # safely encode so we don't have any dots in field names
                stats[encode_float_key(k)] = v
            stats["mean"] = mean
            stats["unit"] = unit
            return stats
        else:
            return {}


class GlobalStats:
    def __init__(self, d=None):
        self.op_metrics = self.v(d, "op_metrics", default=[])
        self.total_time = self.v(d, "total_time")
        self.total_time_per_shard = self.v(d, "total_time_per_shard", default={})
        self.indexing_throttle_time = self.v(d, "indexing_throttle_time")
        self.indexing_throttle_time_per_shard = self.v(d, "indexing_throttle_time_per_shard", default={})
        self.merge_time = self.v(d, "merge_time")
        self.merge_time_per_shard = self.v(d, "merge_time_per_shard", default={})
        self.merge_count = self.v(d, "merge_count")
        self.refresh_time = self.v(d, "refresh_time")
        self.refresh_time_per_shard = self.v(d, "refresh_time_per_shard", default={})
        self.refresh_count = self.v(d, "refresh_count")
        self.flush_time = self.v(d, "flush_time")
        self.flush_time_per_shard = self.v(d, "flush_time_per_shard", default={})
        self.flush_count = self.v(d, "flush_count")
        self.merge_throttle_time = self.v(d, "merge_throttle_time")
        self.merge_throttle_time_per_shard = self.v(d, "merge_throttle_time_per_shard", default={})
        self.ml_processing_time = self.v(d, "ml_processing_time", default=[])

        self.young_gc_time = self.v(d, "young_gc_time")
        self.young_gc_count = self.v(d, "young_gc_count")
        self.old_gc_time = self.v(d, "old_gc_time")
        self.old_gc_count = self.v(d, "old_gc_count")
        self.zgc_cycles_gc_time = self.v(d, "zgc_cycles_gc_time")
        self.zgc_cycles_gc_count = self.v(d, "zgc_cycles_gc_count")
        self.zgc_pauses_gc_time = self.v(d, "zgc_pauses_gc_time")
        self.zgc_pauses_gc_count = self.v(d, "zgc_pauses_gc_count")

        self.memory_segments = self.v(d, "memory_segments")
        self.memory_doc_values = self.v(d, "memory_doc_values")
        self.memory_terms = self.v(d, "memory_terms")
        self.memory_norms = self.v(d, "memory_norms")
        self.memory_points = self.v(d, "memory_points")
        self.memory_stored_fields = self.v(d, "memory_stored_fields")
        self.dataset_size = self.v(d, "dataset_size")
        self.store_size = self.v(d, "store_size")
        self.translog_size = self.v(d, "translog_size")
        self.segment_count = self.v(d, "segment_count")

        self.total_transform_search_times = self.v(d, "total_transform_search_times")
        self.total_transform_index_times = self.v(d, "total_transform_index_times")
        self.total_transform_processing_times = self.v(d, "total_transform_processing_times")
        self.total_transform_throughput = self.v(d, "total_transform_throughput")

        self.ingest_pipeline_cluster_count = self.v(d, "ingest_pipeline_cluster_count")
        self.ingest_pipeline_cluster_time = self.v(d, "ingest_pipeline_cluster_time")
        self.ingest_pipeline_cluster_failed = self.v(d, "ingest_pipeline_cluster_failed")

        self.disk_usage_total = self.v(d, "disk_usage_total")
        self.disk_usage_inverted_index = self.v(d, "disk_usage_inverted_index")
        self.disk_usage_stored_fields = self.v(d, "disk_usage_stored_fields")
        self.disk_usage_doc_values = self.v(d, "disk_usage_doc_values")
        self.disk_usage_points = self.v(d, "disk_usage_points")
        self.disk_usage_norms = self.v(d, "disk_usage_norms")
        self.disk_usage_term_vectors = self.v(d, "disk_usage_term_vectors")

    def as_dict(self):
        return self.__dict__

    def as_flat_list(self):
        def op_metrics(op_item, key, single_value=False):
            doc = {"task": op_item["task"], "operation": op_item["operation"], "name": key}
            if single_value:
                doc["value"] = {"single": op_item[key]}
            else:
                doc["value"] = op_item[key]
            if "meta" in op_item:
                doc["meta"] = op_item["meta"]
            return doc

        all_results = []
        for metric, value in self.as_dict().items():
            if metric == "op_metrics":
                for item in value:
                    if "throughput" in item:
                        all_results.append(op_metrics(item, "throughput"))
                    if "latency" in item:
                        all_results.append(op_metrics(item, "latency"))
                    if "service_time" in item:
                        all_results.append(op_metrics(item, "service_time"))
                    if "processing_time" in item:
                        all_results.append(op_metrics(item, "processing_time"))
                    if "error_rate" in item:
                        all_results.append(op_metrics(item, "error_rate", single_value=True))
                    if "duration" in item:
                        all_results.append(op_metrics(item, "duration", single_value=True))
            elif metric == "ml_processing_time":
                for item in value:
                    all_results.append(
                        {
                            "job": item["job"],
                            "name": "ml_processing_time",
                            "value": {"min": item["min"], "mean": item["mean"], "median": item["median"], "max": item["max"]},
                        }
                    )
            elif metric.startswith("total_transform_") and value is not None:
                for item in value:
                    all_results.append({"id": item["id"], "name": metric, "value": {"single": item["mean"]}})
            elif metric.startswith("disk_usage_") and value is not None:
                for item in value:
                    all_results.append({"index": item["index"], "field": item["field"], "name": metric, "value": {"single": item["value"]}})
            elif metric.endswith("_time_per_shard"):
                if value:
                    all_results.append({"name": metric, "value": value})
            elif value is not None:
                result = {"name": metric, "value": {"single": value}}
                all_results.append(result)
        # sorting is just necessary to have a stable order for tests. As we just have a small number of metrics, the overhead is neglible.
        return sorted(all_results, key=lambda m: m["name"])

    def v(self, d, k, default=None):
        return d.get(k, default) if d else default

    def add_op_metrics(self, task, operation, throughput, latency, service_time, processing_time, error_rate, duration, meta):
        doc = {
            "task": task,
            "operation": operation,
            "throughput": throughput,
            "latency": latency,
            "service_time": service_time,
            "processing_time": processing_time,
            "error_rate": error_rate,
            "duration": duration,
        }
        if meta:
            doc["meta"] = meta
        self.op_metrics.append(doc)

    def tasks(self):
        # ensure we can read race.json files before Rally 0.8.0
        return [v.get("task", v["operation"]) for v in self.op_metrics]

    def metrics(self, task):
        # ensure we can read race.json files before Rally 0.8.0
        for r in self.op_metrics:
            if r.get("task", r["operation"]) == task:
                return r
        return None


class SystemStatsCalculator:
    def __init__(self, store, node_name):
        self.store = store
        self.logger = logging.getLogger(__name__)
        self.node_name = node_name

    def __call__(self):
        result = SystemStats()
        self.logger.debug("Calculating system metrics for [%s]", self.node_name)
        self.logger.debug("Gathering disk metrics.")
        self.add(result, "final_index_size_bytes", "index_size")
        self.add(result, "disk_io_write_bytes", "bytes_written")
        self.logger.debug("Gathering node startup time metrics.")
        self.add(result, "node_startup_time", "startup_time")
        return result

    def add(self, result, raw_metric_key, summary_metric_key):
        metric_value = self.store.get_one(raw_metric_key, node_name=self.node_name)
        metric_unit = self.store.get_unit(raw_metric_key, node_name=self.node_name)
        if metric_value:
            self.logger.debug("Adding record for [%s] with value [%s].", raw_metric_key, str(metric_value))
            result.add_node_metrics(self.node_name, summary_metric_key, metric_value, metric_unit)
        else:
            self.logger.debug("Skipping incomplete [%s] record.", raw_metric_key)


class SystemStats:
    def __init__(self, d=None):
        self.node_metrics = self.v(d, "node_metrics", default=[])

    def v(self, d, k, default=None):
        return d.get(k, default) if d else default

    def add_node_metrics(self, node, name, value, unit):
        metric = {"node": node, "name": name, "value": value}
        if unit:
            metric["unit"] = unit
        self.node_metrics.append(metric)

    def as_flat_list(self):
        all_results = []
        for v in self.node_metrics:
            all_results.append({"node": v["node"], "name": v["name"], "value": {"single": v["value"]}})
        # Sort for a stable order in tests.
        return sorted(all_results, key=lambda m: m["name"])
