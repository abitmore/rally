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

import logging
import os

from elastic_transport import ApiError, TransportError
from jinja2 import Environment, FileSystemLoader

from esrally import PROGRAM_NAME, types
from esrally.client import factory
from esrally.tracker import corpus, index
from esrally.utils import console, io


def process_template(templates_path, template_filename, template_vars, output_path):
    env = Environment(loader=FileSystemLoader(templates_path))
    template = env.get_template(template_filename)

    with open(output_path, "w") as f:
        f.write(template.render(template_vars))


def extract_indices_from_data_streams(client, data_streams_to_extract):
    indices = []
    # first extract index metadata (which is cheap) and defer extracting data to reduce the potential for
    # errors due to invalid index names late in the process.
    for data_stream_name in data_streams_to_extract:
        try:
            indices += index.extract_indices_from_data_stream(client, data_stream_name)
        except (ApiError, TransportError):
            logging.getLogger(__name__).exception("Failed to extract indices from data stream [%s]", data_stream_name)

    return indices


def extract_mappings_and_corpora(client, output_path, indices_to_extract, batch_size):
    indices = []
    corpora = []
    # first extract index metadata (which is cheap) and defer extracting data to reduce the potential for
    # errors due to invalid index names late in the process.
    for index_name in indices_to_extract:
        try:
            indices += index.extract(client, output_path, index_name)
        except (ApiError, TransportError):
            logging.getLogger(__name__).exception("Failed to extract index [%s]", index_name)

    # That list only contains valid indices (with index patterns already resolved)
    for i in indices:
        c = corpus.extract(client, output_path, i["name"], batch_size)
        if c:
            corpora.append(c)

    return indices, corpora


def create_track(cfg: types.Config):
    logger = logging.getLogger(__name__)

    track_name = cfg.opts("track", "track.name")
    indices = cfg.opts("generator", "indices")
    root_path = cfg.opts("generator", "output.path")
    target_hosts = cfg.opts("client", "hosts").default
    client_options = cfg.opts("client", "options").default
    data_streams = cfg.opts("generator", "data_streams")
    batch_size = int(cfg.opts("generator", "batch_size"))

    distribution_flavor, distribution_version, _, _ = factory.cluster_distribution_version(target_hosts, client_options)
    client = factory.EsClientFactory(
        hosts=target_hosts,
        client_options=client_options,
        distribution_version=distribution_version,
        distribution_flavor=distribution_flavor,
    ).create()

    console.info(f"Connected to Elasticsearch cluster version [{distribution_version}] flavor [{distribution_flavor}] \n", logger=logger)

    output_path = os.path.abspath(os.path.join(io.normalize_path(root_path), track_name))
    io.ensure_dir(output_path)
    challenge_path = os.path.abspath(os.path.join(output_path, "challenges"))
    io.ensure_dir(challenge_path)
    operations_path = os.path.abspath(os.path.join(output_path, "operations"))
    io.ensure_dir(operations_path)

    if data_streams is not None:
        logger.info("Creating track [%s] matching data streams [%s]", track_name, data_streams)
        extracted_indices = extract_indices_from_data_streams(client, data_streams)
        indices = extracted_indices
    logger.info("Creating track [%s] matching indices [%s]", track_name, indices)

    indices, corpora = extract_mappings_and_corpora(client, output_path, indices, batch_size)
    if len(indices) == 0:
        raise RuntimeError("Failed to extract any indices for track!")

    template_vars = {"track_name": track_name, "indices": indices, "corpora": corpora}

    track_path = os.path.join(output_path, "track.json")
    default_challenges = os.path.join(challenge_path, "default.json")
    default_operations = os.path.join(operations_path, "default.json")
    templates_path = os.path.join(cfg.opts("node", "rally.root"), "resources")
    process_template(templates_path, "track.json.j2", template_vars, track_path)
    process_template(templates_path, "challenges.json.j2", template_vars, default_challenges)
    process_template(templates_path, "operations.json.j2", template_vars, default_operations)

    console.println("")
    console.info(f"Track {track_name} has been created. Run it with: {PROGRAM_NAME} race --track-path={output_path}")
