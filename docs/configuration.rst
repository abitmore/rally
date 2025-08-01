Configuration
=============

Rally Configuration
-------------------

Rally stores its configuration in the file ``~/.rally/rally.ini`` which is automatically created the first time Rally is executed. It comprises the following sections.

.. note:: 
    The configuration file can use `${CONFIG_DIR}` to refer to the directory where Rally stores its configuration files. This is useful for configuring Rally in a portable way.
    This defaults to `~/.rally`, but can be overridden by setting the `RALLY_HOME` environment variable in your shell.

meta
~~~~

This section contains meta information about the configuration file.

* ``config.version``: The version of the configuration file format. This property is managed by Rally and should not be changed.

.. _system:

system
~~~~~~

This section contains global information for the current benchmark environment. This information should be identical on all machines where Rally is installed.

* ``env.name`` (default: "local"): The name of this benchmark environment. It is used as meta-data in metrics documents if an Elasticsearch metrics store is configured. Only alphanumeric characters are allowed.
* ``available.cores`` (default: number of logical CPU cores): Determines the number of available CPU cores. Rally aims to create one asyncio event loop per core and will distribute clients evenly across event loops.
* ``async.debug`` (default: false): Enables debug mode on Rally's internal `asyncio event loop <https://docs.python.org/3/library/asyncio-eventloop.html#enabling-debug-mode>`_. This setting is mainly intended for troubleshooting.
* ``passenv`` (default: "PATH"): A comma-separated list of environment variable names that should be passed to the Elasticsearch process.

node
~~~~

This section contains machine-specific information.

* ``root.dir`` (default: "~/.rally/benchmarks"): Rally uses this directory to store all benchmark-related data. It assumes that it has complete control over this directory and any of its subdirectories.
* ``src.root.dir`` (default: "~/.rally/benchmarks/src"): The directory where the source code of Elasticsearch or any plugins is checked out. Only relevant for benchmarks from sources.

source
~~~~~~

This section contains more details about the source tree.

* ``remote.repo.url`` (default: "https://github.com/elastic/elasticsearch.git"): The URL from which to checkout Elasticsearch.
* ``elasticsearch.src.subdir`` (default: "elasticsearch"): The local path, relative to ``src.root.dir``, of the Elasticsearch source tree.
* ``cache`` (default: true): Enables Rally's internal :ref:`source artifact <pipelines_from-sources>` cache (``elasticsearch*.tar.gz`` and optionally ``*.zip`` files for plugins). Artifacts are cached based on their git revision.
* ``cache.days`` (default: 7): The number of days for which an artifact should be kept in the source artifact cache.

.. _configuration_source:

benchmarks
~~~~~~~~~~

This section contains details about the benchmark data directory.

* ``local.dataset.cache`` (default: "~/.rally/benchmarks/data"): The directory in which benchmark data sets are stored. Depending on the benchmarks that are executed, this directory may contain hundreds of GB of data.

.. _configuration_reporting:

reporting
~~~~~~~~~

This section defines how metrics are stored.

* ``datastore.type`` (default: "in-memory"): If set to "in-memory" all metrics will be kept in memory while running the benchmark. If set to "elasticsearch" all metrics will instead be written to a persistent metrics store and the data are available for further analysis.
* ``sample.queue.size`` (default: 2^20): The number of metrics samples that can be stored in Rally's in-memory queue.
* ``metrics.request.downsample.factor`` (default: 1): Determines how many service time and latency samples should be kept in the metrics store. By default all values will be kept. To keep only e.g. every 100th sample, specify 100. This is useful to avoid overwhelming the metrics store in benchmarks with many clients (tens of thousands).
* ``output.processingtime`` (default: false): If set to "true", Rally will show the additional metric :ref:`processing time <summary_report_processing_time>` in the command line report.

The following settings are applicable only if ``datastore.type`` is set to "elasticsearch":

* ``datastore.host``: The host name of the metrics store, e.g. "10.17.20.33".
* ``datastore.port``: The port of the metrics store, e.g. "9200".
* ``datastore.secure``: If set to ``false``, Rally assumes a HTTP connection. If set to ``true``, it assumes a HTTPS connection.
* ``datastore.ssl.verification_mode`` (default: "full"): By default the metric store's SSL certificate is checked ("full"). To disable certificate verification set this value to "none".
* ``datastore.ssl.certificate_authorities`` (default: empty): Determines the path on the local file system to the certificate authority's signing certificate.
* ``datastore.user``: Sets the name of the Elasticsearch BASIC authentication user for the metrics store.
* ``datastore.password``: Sets the password of the Elasticsearch BASIC authentication user for the metrics store. Alternatively, this password can be configured using the ``RALLY_REPORTING_DATASTORE_PASSWORD`` environment variable, which avoids storing credentials in a plain text file. The environment variable will take precedence over the config file if both define a password.
* ``datastore.api_key``: Sets the Elasticsearch API key for the metrics store to be used instead of BASIC authentication. Alternatively, the API key can be configured using the ``RALLY_REPORTING_DATASTORE_API_KEY`` environment variable, which avoids storing credentials in a plain text file. The environment variable will take precedence over the config file if both are defined. Configuration of both (``datastore.user`` and ``datastore.password``) and ``datastore.api_key`` is not allowed. If both are configured, Rally will raise an error. Required for authentication with Elastic Cloud Serverless projects.
* ``datastore.probe.cluster_version`` (default: true): Enables automatic detection of the metric store's version.
* ``datastore.number_of_shards`` (default: `Elasticsearch default value <https://www.elastic.co/guide/en/elasticsearch/reference/current/index-modules.html#_static_index_settings>`_): The number of primary shards that the ``rally-*`` indices should have. Any updates to this setting after initial index creation will only be applied to new ``rally-*`` indices. An error is raised if set for Elastic Cloud Serverless projects.
* ``datastore.number_of_replicas`` (default: `Elasticsearch default value <https://www.elastic.co/guide/en/elasticsearch/reference/current/index-modules.html#_static_index_settings>`_): The number of replicas each primary shard has. Defaults to . Any updates to this setting after initial index creation will only be applied to new ``rally-*`` indices. An error is raised if set for Elastic Cloud Serverless projects.
* ``datastore.overwrite_existing_templates`` (default: ``false``): Existing Rally index templates are replaced only when this option is ``true``.


**Examples**

Define an unprotected metrics store in the local network::

    [reporting]
    datastore.type = elasticsearch
    datastore.host = 192.168.10.17
    datastore.port = 9200
    datastore.secure = false
    datastore.user =
    datastore.password =

Define a secure connection to a metrics store in the local network with a self-signed certificate::

    [reporting]
    datastore.type = elasticsearch
    datastore.host = 192.168.10.22
    datastore.port = 9200
    datastore.secure = true
    datastore.ssl.verification_mode = none
    datastore.user = rally
    datastore.password = the-password-to-your-cluster

Define a secure connection to an Elastic Cloud cluster::

    [reporting]
    datastore.type = elasticsearch
    datastore.host = 123456789abcdef123456789abcdef1.europe-west4.gcp.elastic-cloud.com
    datastore.port = 9243
    datastore.secure = true
    datastore.user = rally
    datastore.password = the-password-to-your-cluster

Define a secure connection to an Elastic Cloud Serverless project::

    [reporting]
    datastore.type = elasticsearch
    datastore.host = rally-metrics-123abc.es.us-east-1.aws.elastic.cloud
    datastore.port = 443
    datastore.secure = true
    datastore.api_key = YzAAa09aG0JIT1yPOU8ydHljQES6aFNicFdhRGM3dTFLMDVITnNRFmNNdw==

storage
~~~~~~~

This section defines how client is configured to transfer corpus files. The main
advantages of this downloader implementation are:

* It supports configuring multiple mirror URLs. The client will load balance between these URLs giving priority to
  those with the lower latency. In case of failures the client will download files from the original URL.
* It supports multipart downloading of pieces of files from multiple servers at the same time, mitigating the impact of
  network failures. Those file parts that experience errors downloading will be eventually re-downloaded from
  another server or, as the very last resource, from the original source.
* It supports local caching of files and it can resume downloads when connections are interrupted. The download state
  is preserved between Rally executions, allowing downloads to continue from where they left off rather than starting
  over.

.. warning::

    This transfers manager implementation is experimental and under active development.

Configuration options are:

* ``storage.adapters`` is a comma-separated list of storage adapter implementations specified using the following
  format:

  ``<python module name>:<adapter class name>``

  Here is an example of valid value for http(s) adapter::

    [storage]
    storage.adapters = esrally.storage._http:HTTPAdapter


  At this point in time `esrally.storage._http:HTTPAdapter` is the only existing `Adapter` implementations intended
  for public use and that is already the default one. So it is required to edit this option for special customizations.

* ``storage.local_dir`` indicates the default directory where to store local files when no path has been specified.

* ``storage.max_connections`` represents the maximum number of client connections to be made against the same server or
  bucket for each transfer. The default value is 4. In case there will be more unfinished transfers in progress at the
  same time, this value would be dynamically limited by the following formula::

    transfer.max_connections = min(storage.max_connections, (storage.max_workers / number_of_unfinished_transfers) + 1)

* ``storage.max_workers`` indicates the maximum number of worker threads used for making storage files transfers.

* ``storage.mirror_files`` is used to provide a json file that specify the mapping for mirrors URLs resolution.
  Example::

      [storage]
      storage.mirror_files = ~/.rally/storage-mirrors.json

  Example of a JSON file used to specify mirrors servers for downloading rally tracks files to a couple of AWS S3
  buckets::

      {
        "mirrors": [
          {
            "sources": [
              "https://rally-tracks.elastic.co/"
            ],
            "destinations": [
              "https://rally-tracks-eu-central-1.s3.eu-central-1.amazonaws.com/",
              "https://rally-tracks-us-west-1.s3.us-west-1.amazonaws.com/"
            ]
          }
        ]
      }

  The mirroring of the files on mirrors servers has to be provided by the infrastructure. The esrally client will look
  for the files on the destination mirror endpoints URLs or use the original source endpoint URL in case the files
  are not mirrored or they have a different size from the source one. The client will prefer endpoints with the lower
  latency fetching the head of the file.

* ``storage.monitor_interval`` represents the time interval (in seconds) `TransferManager` should wait for consecutive
  monitor operations (log transfer and connections statistics, adjust the maximum number of connections, etc.).

* ``storage.multipart_size`` When the file size measured in bytes is greater than this value the file is split in chunk
  of this size plus the last one ()that could be smaller). Each part will be downloaded separately and in parallel using
  a dedicated connection by a worker thread and eventually from a different mirror server to load balance the network
  traffic between multiple servers. If the resulting number of parts is greater than ``storage.max_workers`` and
  ``storage.max_connections`` options, then the transfer of those parts exceeding these limits will be performed as
  soon as a worker thread gets available or a HTTP connection get released by another thread.

* ``storage.random_seed`` a string used to initialize the client random number generator. This could be used to make
  problems easier to reproduce in continuous integration. In most of the cases it should be left empty.


HTTP Adapter
************

This adapter can be used only to download files from public HTTP or HTTPS servers.

* ``storage.http.chunk_size`` is used to specify the size of the buffer is being used for transferring chunk of files.

* ``storage.http.max_retries`` is used to configure the maximum number of retries for making HTTP adapter requests.
  it accept a numeric value to simply specify total number of retries. Examples::

    [storage]
    storage.http.max_retries = 3

  For a more complex uses it accepts a dictionary of parameters (defined in JSON format) to be passed to the
  `urllib3.Retry`_ class constructor. Example::

    [storage]
    storage.http.max_retries = {"total": 5, "backoff_factor": 5}

  .. _urllib3.Retry: https://urllib3.readthedocs.io/en/stable/reference/urllib3.util.html


S3 Adapter
**********

This adapter can be used only to download files from `S3 Cloud Storage Service`_. It is intended to be used to configure
S3 buckets as mirror for track file downloads.

It requires `Boto3 Client`_ to be installed and it accepts only URLs with the following format::

  s3://<bucket-name>/[<object-key-prefix>]

In the case the boto3 client is not installed, and S3 buckets are publicly readable without authentication, you can use
the HTTP adapter instead, for example by using the following URL format::

  https://<bucket-name>.s3.<region-name>.amazonaws.com/[<object-key-prefix>]

Please look at the `S3 Service Documentation`_ for more details.

Example of ``rally.ini`` configuration::

    [storage]
    storage.mirror_files = ~/.rally/storage-mirrors.json

Example of ``~/.rally/storage-mirrors.json`` file::

    {
        "mirrors": [
            {
                "sources": [
                    "https://rally-tracks.elastic.co/"
                ],
                "destinations": [
                    "s3://rally-tracks-eu-central-1/",
                    "s3://rally-tracks-us-west-1/"
                ]
              }
            ]
          }
    }

Configuration options:

* ``storage.aws.profile`` is used to specify the profile name to be used for connecting to the S3 service. By default
  it will use credentials detected from the environment as specified by 'boto3' client.

  .. _S3 Cloud Storage Service: https://aws.amazon.com/es/s3/
  .. _Boto3 Client: https://boto3.amazonaws.com/v1/documentation/api/latest/index.html
  .. _S3 Service Documentation: https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/DownloadDistS3AndCustomOrigins.html#concept_S3Origin

tracks
~~~~~~

This section defines how :doc:`tracks </track>` are retrieved. All keys are read by Rally using the convention ``<<track-repository-name>>.url``, e.g. ``custom-track-repo.url`` which can be selected the command-line via ``--track-repository="custom-track-repo"``. By default, Rally chooses the track repository specified via ``default.url`` which points to https://github.com/elastic/rally-tracks.

teams
~~~~~

This section defines how :doc:`teams </car>` are retrieved. All keys are read by Rally using the convention ``<<team-repository-name>>.url``, e.g. ``custom-team-repo.url`` which can be selected the command-line via ``--team-repository="custom-team-repo"``. By default, Rally chooses the track repository specified via ``default.url`` which points to https://github.com/elastic/rally-teams.

defaults
~~~~~~~~

This section defines default values for certain command line parameters of Rally.

* ``preserve_benchmark_candidate`` (default: false): Determines whether Elasticsearch installations will be preserved or wiped by default after a benchmark. For preserving an installation for a single benchmark, use the command line flag ``--preserve-install``.

distributions
~~~~~~~~~~~~~

* ``release.cache`` (default: true): Determines whether released Elasticsearch versions should be cached locally.

Proxy Configuration
-------------------

Rally downloads all necessary data automatically for you:

* Elasticsearch distributions from elastic.co if you specify ``--distribution-version=SOME_VERSION_NUMBER``
* Elasticsearch source code from Github if you specify a revision number e.g. ``--revision=952097b``
* Track meta-data from Github
* Track data from a cloud bucket

Hence, it needs to connect via http(s) to the outside world. If you are behind a corporate proxy you need to configure Rally and git. As many other Unix programs, Rally relies that the proxy URL is available in the environment variables ``http_proxy`` (lowercase only), ``https_proxy`` or ``HTTPS_PROXY``, ``all_proxy`` or ``ALL_PROXY``. Hence, you should add this line to your shell profile, e.g. ``~/.bash_profile``::

    export http_proxy=http://proxy.acme.org:8888/

Afterwards, source the shell profile with ``source ~/.bash_profile`` and verify that the proxy URL is correctly set with ``echo $http_proxy``.

Finally, you can set up git (see also the `Git config documentation <https://git-scm.com/docs/git-config>`_)::

    git config --global http.proxy $http_proxy

Verify that the proxy setup for git works correctly by cloning any repository, e.g. the ``rally-tracks`` repository::

    git clone https://github.com/elastic/rally-tracks.git

If the configuration is correct, git will clone this repository. You can delete the folder ``rally-tracks`` after this verification step.

To verify that Rally will connect via the proxy server you can check the log file. If the proxy server is configured successfully, Rally will log the following line on startup::

    Connecting via proxy URL [http://proxy.acme.org:3128/] to the Internet (picked up from the environment variable [http_proxy]).


.. note::

   Rally will use this proxy server only for downloading benchmark-related data. It will not use this proxy for the actual benchmark.

.. _logging:

Logging
-------

Logging in Rally is configured in ``~/.rally/logging.json``. For more information about the log file format please refer to the following documents:

* `Python logging cookbook <https://docs.python.org/3/howto/logging-cookbook.html>`_ provides general tips and tricks.
* The Python reference documentation on the `logging configuration schema <https://docs.python.org/3/library/logging.config.html#logging-config-dictschema>`_ explains the file format.
* The `logging handler documentation <https://docs.python.org/3/library/logging.handlers.html>`_ describes how to customize where log output is written to.

By default, Rally will log all output to ``~/.rally/logs/rally.log`` in plain text format and ``~/.rally/logs/rally.json`` in ECS JSON format.
The default timestamp for ``rally.log`` is UTC, but users can opt for the local time by setting ``"timezone": "localtime"`` in the logging configuration file. 
The ``rally.json`` file is formatted to the ECS format for ease of ingestion with filebeat. See the `ECS Reference <https://www.elastic.co/guide/en/ecs/current/ecs-using-ecs.html>`_ for more information.

There are a number of default options for the ``json`` logger that can be overridden in ``~/.rally/logging.json``. 
First, ``exclude_fields`` will exclude ``log.original`` from the ECS defaults, since it can be quite noisy and superfluous. 
And ``mutators`` is by default set to ``["esrally.log.rename_actor_fields", "esrally.log.rename_async_fields"]`` which will rename ``actorAddress`` and ``taskName`` to ``rally.thespian.actorAddress`` and ``python.asyncio.task`` respectively.

The log file will not be rotated automatically as this is problematic due to Rally's multi-process architecture. Setup an external tool like `logrotate <https://linux.die.net/man/8/logrotate>`_ to achieve that. See the following example as a starting point for your own ``logrotate`` configuration and ensure to replace the path ``/home/user/.rally/logs/rally.log`` with the proper one::

    /home/user/.rally/logs/rally.log {
            # rotate daily
            daily
            # keep the last seven log files
            rotate 7
            # remove logs older than 14 days
            maxage 14
            # compress old logs ...
            compress
            # ... after moving them
            delaycompress
            # ignore missing log files
            missingok
            # don't attempt to rotate empty ones
            notifempty
    }

Example
~~~~~~~

With the following configuration Rally will log all output to standard error, and format the timestamps in the local timezone::

    {
      "version": 1,
      "formatters": {
        "normal": {
          "format": "%(asctime)s,%(msecs)d %(actorAddress)s/PID:%(process)d %(name)s %(levelname)s %(message)s",
          "datefmt": "%Y-%m-%d %H:%M:%S",
          "timezone": "localtime",
          "()": "esrally.log.configure_utc_formatter"
        }
      },
      "filters": {
        "isActorLog": {
          "()": "thespian.director.ActorAddressLogFilter"
        }
      },
      "handlers": {
        "console_log_handler": {
          "class": "logging.StreamHandler",
          "formatter": "normal",
          "filters": ["isActorLog"]
        }
      },
      "root": {
        "handlers": ["console_log_handler"],
        "level": "INFO"
      },
      "loggers": {
        "elasticsearch": {
          "handlers": ["console_log_handler"],
          "level": "WARNING",
          "propagate": false
        }
      }
    }

Portability
~~~~~~~~~~~

You can also use ``${LOG_PATH}`` in the ``"filename"`` value of the handler you are configuring to make the log configuration more portable.
Rally will substitute ``${LOG_PATH}`` with the path to the directory where Rally stores its log files. By default, this is ``~/.rally/logs``. 
But this can be overridden by setting the ``RALLY_HOME`` environment variable in your shell, and logs will be stored in ``${RALLY_HOME}/logs``.

NOTE:: This is only supported with the ``esrally.log.configure_file_handler`` and ``esrally.log.configure_profile_file_handler`` handlers.

Here is an example of a logging configuration that uses ``${LOG_PATH}``::

    {
      "version": 1,
      "formatters": {
        "normal": {
          "format": "%(asctime)s,%(msecs)d %(actorAddress)s/PID:%(process)d %(name)s %(levelname)s %(message)s",
          "datefmt": "%Y-%m-%d %H:%M:%S",
          "()": "esrally.log.configure_utc_formatter"
        }
      },
      "handlers": {
        "rally_log_handler": {
          "()": "esrally.log.configure_file_handler", # <-- use configure_file_handler or configure_profile_file_handler
          "filename": "${LOG_PATH}/rally.log", # <-- use ${LOG_PATH} here
          "encoding": "UTF-8",
          "formatter": "normal"
        }
      },
      "root": {
        "handlers": ["rally_log_handler"],
        "level": "INFO"
      },
      "loggers": {
        "elasticsearch": {
          "handlers": ["rally_log_handler"],
          "level": "WARNING",
          "propagate": false
        }
      }
    }


Example
~~~~~~~

With the following configuration Rally will log to ``~/.rally/logs/rally.log`` and ``~/.rally/logs/rally.json``, the 
latter being a JSON file. 

The ``mutators`` property is optional and defaults to ``["esrally.log.rename_actor_fields", "esrally.log.rename_async_fields"]``.
Similarly, the ``exclude_fields`` property is optional and defaults to ``["log.original"]``::

    {
      "version": 1,
      "formatters": {
        "normal": {
          "format": "%(asctime)s,%(msecs)d %(actorAddress)s/PID:%(process)d %(name)s %(levelname)s %(message)s",
          "datefmt": "%Y-%m-%d %H:%M:%S",
          "()": "esrally.log.configure_utc_formatter"
        },
        "json": {
          "format": "%(message)s",
          "exclude_fields": [
            "log.original"
          ],
          "mutators": [
            "esrally.log.rename_actor_fields",
            "esrally.log.rename_async_fields"
          ],
          "()": "esrally.log.configure_ecs_formatter"
        }
      },
      "handlers": {
        "rally_log_handler": {
          "()": "esrally.log.configure_file_handler",
          "filename": "${LOG_PATH}/rally.log",
          "encoding": "UTF-8",
          "formatter": "normal"
        },
        "rally_json_handler": {
          "()": "esrally.log.configure_file_handler",
          "filename": "${LOG_PATH}/rally.json",
          "encoding": "UTF-8",
          "formatter": "json"
        }
      },
      "root": {
        "handlers": ["rally_log_handler", "rally_json_handler"],
        "level": "INFO"
      },
      "loggers": {
        "elasticsearch": {
          "handlers": ["rally_log_handler", "rally_json_handler"],
          "level": "WARNING",
          "propagate": false
        }
      }
    }

