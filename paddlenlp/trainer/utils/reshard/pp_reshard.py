# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import math
import re
from collections import OrderedDict

from paddle.distributed.fleet.utils.log_util import logger

from .common import NodeModelState


def extract_param_names_groupby_layer(
    meta,
    mp_rank=0,
):
    param_names_by_layer = OrderedDict()
    assert "parallel_config" in meta
    parallel_config = meta["parallel_config"]
    assert "pp_degree" in parallel_config
    pp_degree = int(parallel_config["pp_degree"])
    sharding_metas = meta["sharding_metas"]
    for pp_rank in range(pp_degree):
        suffix = f"tp{mp_rank:0>2d}_pp{pp_rank:0>2d}"
        assert suffix in sharding_metas
        assert "structure_name_mapping" in sharding_metas[suffix]
        name_mapping = sharding_metas[suffix]["structure_name_mapping"]
        for (k, v) in name_mapping.items():
            layer_name = extract_layer_name(k)
            if layer_name not in param_names_by_layer:
                param_names_by_layer[layer_name] = []
            param_names_by_layer[layer_name].append((k, v))
    return param_names_by_layer


def build_pipeline_context(meta, transformer_layer_num, dst_pp, dst_vpp, segment_method, mp_rank=0):
    layer_params = extract_param_names_groupby_layer(meta, mp_rank)
    # 2、rename tensor names
    pipeline_context = PipeLineSegmentContext(
        transformer_layer_num,
        dst_pp,
        dst_vpp,
        segment_method,
        layer_params,
    )
    return pipeline_context


class LayerNameScope:
    prefix_to_template = OrderedDict()
    prefix_to_template["column_sequence_parallel_linear"] = "column_sequence_parallel_linear_{}"
    prefix_to_template["row_sequence_parallel_linear"] = "row_sequence_parallel_linear_{}"
    prefix_to_template["linear"] = "linear_{}"
    prefix_to_template["layer_norm"] = "layer_norm_{}"
    prefix_to_template["embedding"] = "embedding_{}"
    prefix_to_template["create_parameter"] = "create_parameter_{}"
    prefix_to_template["ernie_lm_head"] = "ernie_lm_head_{}"

    def __init__(self, prefix, template):
        self.prefix = prefix
        self.last_layer_id = ""
        self.last_old_layer_name = ""
        self.template = template
        self.index = -1
        self.sub_scopes = OrderedDict()

    @classmethod
    def create_sub_scope(cls, prefix, old_layer_name):
        for (k, v) in cls.prefix_to_template.items():
            if old_layer_name.startswith(k):
                return LayerNameScope(prefix, v)
        return None

    @classmethod
    def get_layer_prefix(cls, old_layer_name):
        for k in cls.prefix_to_template:
            if old_layer_name.startswith(k):
                return k
        return None

    def get_next_scope(self, layer_id, old_layer_name):
        if old_layer_name != self.last_old_layer_name or layer_id != self.last_layer_id:
            self.index = self.index + 1
            self.last_old_layer_name = old_layer_name
            self.last_layer_id = layer_id
            self.sub_scopes = OrderedDict()
        return self

    def get_layer_name(self):
        name = ""
        if self.template:
            name = self.template.format(self.index)
        if self.prefix:
            name = self.prefix + "_" + name
        return name

    def get_sub_scope(self, sub_layer_name):
        layer_prefix = self.get_layer_prefix(sub_layer_name)
        assert layer_prefix, f"{sub_layer_name} invalid, prefix {self.prefix}"
        if layer_prefix in self.sub_scopes:
            return self.sub_scopes[layer_prefix]
        layer_template = self.prefix_to_template[layer_prefix]
        prefix = self.get_layer_name()
        scope = LayerNameScope(prefix, layer_template)
        self.sub_scopes[layer_prefix] = scope
        return scope


class LayerReNamingManager:
    def __init__(self):
        self.top_layer_name_scope = LayerNameScope(None, None)

    def extract_layer_names(self, full_layer_name):
        return [full_layer_name]

    def get_new_layer_name(self, layer_id: str, old_name: str):
        layers = self.extract_layer_names(old_name)
        name_scope = self.top_layer_name_scope
        for l in layers:
            name_scope = name_scope.get_sub_scope(l).get_next_scope(layer_id, l)
        return name_scope.get_layer_name()

    def get_new_param_name(self, layer_id, old_name: str):
        names = old_name.split(".")
        layer_name = self.get_new_layer_name(layer_id, names[0])
        names[0] = layer_name
        return ".".join(names)


def extract_layer_name(param_name):
    first_layer_pattern = r"^ernie\.embed_tokens"
    last_layer_pattern1 = "^ernie\.norm"
    last_layer_pattern2 = r"^lm_head"
    pattern = r"^ernie\.layers((\.\d+))"

    # match 1
    for p in [
        pattern,
        first_layer_pattern,
        last_layer_pattern1,
        last_layer_pattern2,
    ]:
        match = re.search(p, param_name)
        if match:
            return match.group()
    return None


def index_layer(layer_name, transformer_layer_num=None):
    if transformer_layer_num is None:
        transformer_layer_num = 1000
    if layer_name == "ernie.embed_tokens":
        return 0
    elif layer_name == "ernie.norm":
        return transformer_layer_num + 1
    elif layer_name == "lm_head":
        return transformer_layer_num + 2
    else:
        pattern = r"ernie\.layers((\.(\d+)))"
        match = re.search(pattern, layer_name)
        assert match
        return int(match.group(3)) + 1


class PipeLinelayer:
    def __init__(self, layer_name, param_names):
        self._layer_name = layer_name
        # make sure name with the same sublayer type is ordered
        param_names = sorted(param_names, key=lambda x: x[1])
        self._params = OrderedDict()
        for (k, v) in param_names:
            self._params[k] = v

    @property
    def params(self):
        return self._params

    @property
    def name(self):
        return self._layer_name


class PipeLineSegment:
    def __init__(self, start_index, end_index):
        self._start_index = start_index
        self._end_index = end_index
        self._cur_index = start_index
        self._layers = OrderedDict()

    def add_layer(self, layer_name, param_names):
        assert self._cur_index < self._end_index
        layer = PipeLinelayer(layer_name, param_names)
        self._layers[layer_name] = layer
        self._cur_index = self._cur_index + 1

    @property
    def layers(self):
        assert self._cur_index == self._end_index
        return self._layers


class PipeLineStage:
    def __init__(self):
        self._rename_mgr = LayerReNamingManager()
        # map segement start index to segment
        self._segments = OrderedDict()
        self._layer_to_segment = OrderedDict()
        self._param_to_tname = OrderedDict()

    def add_segment(self, start_index, end_index):
        segment = PipeLineSegment(start_index, end_index)
        self._segments[start_index] = segment
        for i in range(start_index, end_index):
            self._layer_to_segment[i] = segment

    def add_layer(self, layer_index, layer_name, param_names):
        assert layer_index in self._layer_to_segment
        segment = self._layer_to_segment[layer_index]
        segment.add_layer(layer_name, param_names)

    def build_name_mapping(self):
        for (k, segment) in self._segments.items():
            for (i, layer) in segment.layers.items():
                for param in layer.params.items():
                    (param_name, tensor_name) = param
                    # map to a new name
                    n_name = self._rename_mgr.get_new_param_name(layer.name, tensor_name)
                    logger.info(f"{param_name} {tensor_name}=>{n_name}")
                    self._param_to_tname[param_name] = (tensor_name, n_name)

    def map_name(self, param_name, t_name):
        assert param_name in self._param_to_tname
        tensor_name, n_name = self._param_to_tname[param_name]
        assert tensor_name == t_name
        return n_name

    def print_name_mapping(self):
        for (name, mapping) in self._param_to_tname.items():
            print(f"{name} mapping {mapping[0]} => {mapping[1]}\n")


# segment context for pp X sharding
class PipeLineSegmentContext:
    def __init__(
        self,
        transformer_layer_num,
        pp_degree,
        vpp_degree,
        segment_method,
        param_names_by_layer,
    ):
        self._transformer_layer_num = transformer_layer_num
        self._pp_degree = pp_degree
        self._vpp_degree = vpp_degree
        self._segment_method = segment_method
        self._layers = list(param_names_by_layer.keys())
        self._stages = []
        self._layer_index_to_stage = {}
        self._layer_name_to_index = {}
        self._layer_index_to_name = {}
        self._layer_name_to_stage = {}
        self._param_names_by_layer = param_names_by_layer

        self._index_layers()

        stage_segments = self._segment()
        for (i, stage_seg) in enumerate(stage_segments):
            pipe_stage = PipeLineStage()
            self._stages.append(pipe_stage)
            for seg in stage_seg:
                pipe_stage.add_segment(seg[0], seg[1])
                for j in range(*seg):
                    assert j in self._layer_index_to_name
                    layer_name = self._layer_index_to_name[j]
                    assert layer_name in self._param_names_by_layer
                    pipe_stage.add_layer(j, layer_name, self._param_names_by_layer[layer_name])
                    self._layer_index_to_stage[j] = i
                    self._layer_name_to_stage[layer_name] = i

        for stage in self._stages:
            stage.build_name_mapping()

    def _index_layers(self):
        for layer_name in self._param_names_by_layer.keys():
            index = index_layer(layer_name, self.transformer_layer_num)
            self._layer_name_to_index[layer_name] = index
            self._layer_index_to_name[index] = layer_name

    @property
    def transformer_layer_num(self):
        if self._transformer_layer_num and self._transformer_layer_num > 0:
            assert len(self._param_names_by_layer) == self._transformer_layer_num + 3
            return self._transformer_layer_num
        else:
            # assume model is of the structure below
            # embedding -> n*(transformer layer) -> 2 output layer
            return len(self._param_names_by_layer) - 3

    def _segment(self):
        layer_num = self.transformer_layer_num + 3  # ?
        stage_num = self._pp_degree * self._vpp_degree

        # segment by weights
        def segment_by_layer():
            # assume model is of the structure below
            # embedding -> n*(transformer layer) -> 2 output layer
            # segment index
            weights = [0 for _ in range(layer_num)]
            non_zero_layers = range(1, layer_num - 2)
            for i in non_zero_layers:
                weights[i] = 1

            part_size = sum(weights) // stage_num
            result = [0 for _ in range(stage_num + 1)]
            memory_counter = 0
            result_idx = 1
            for idx, weight in enumerate(weights):
                memory_counter += weight
                if memory_counter == part_size:
                    result[result_idx] = idx + 1
                    result_idx += 1
                    memory_counter = 0
            result[stage_num] = layer_num
            return result

        def segment_uniform():
            result = [0 for _ in range(stage_num + 1)]
            part_size = math.floor(layer_num / stage_num)
            extra_layers = layer_num % stage_num
            for i in range(1, stage_num):
                offset = 1 if i > (stage_num - extra_layers) else 0
                result[i] = int(min(result[i - 1] + part_size + offset, layer_num))
            result[stage_num] = layer_num
            return result

        result = segment_uniform() if (self._segment_method == "uniform") else segment_by_layer()
        index_segments = [[] for _ in range(self._pp_degree)]
        for i in range(stage_num):
            index_segments[i % self._pp_degree].append((result[i], result[i + 1]))
        print(f"segment results {index_segments}")
        return index_segments

    def map_name(self, param_name, t_name):
        layer_name = extract_layer_name(param_name)
        assert layer_name in self._layer_name_to_index
        layer_index = self._layer_name_to_index[layer_name]
        stage_index = self._layer_index_to_stage[layer_index]
        stage = self._stages[stage_index]
        return stage.map_name(param_name, t_name)

    def map_name_to_stage(self, name):
        layer_name = extract_layer_name(name)
        assert layer_name in self._layer_name_to_index
        layer_index = self._layer_name_to_index[layer_name]
        stage_index = self._layer_index_to_stage[layer_index]
        return stage_index

    """
    def map_name_to_stage(self, param_name):
        layer_name = extract_layer_name(param_name)
        assert layer_name in self._layer_name_to_stage
        stage_index = self._layer_name_to_stage[layer_name]
        return stage_index
    """

    def segment_layers(self, layer_names):
        layer_segments = [[] for _ in range(self._pp_degree)]
        for layer_name in layer_names:
            assert layer_name in self._layer_name_to_index
            layer_index = self._layer_name_to_index[layer_name]
            stage_index = self._layer_index_to_stage[layer_index]
            layer_segments[stage_index].append((layer_index, layer_name))
        layer_segments = [[ee[1] for ee in sorted(e)] for e in layer_segments]
        return layer_segments

    def print_name_mapping(self):
        for (i, stage) in enumerate(self._stages):
            print(f"{'='*30}stage {i} {'='*30}")
            stage.print_name_mapping()


def convert_pp_in_group(hcg, sharding_rank, src_stage_num, pp_line_context, state_cache):
    pp_degree = hcg.get_pipe_parallel_world_size()
    pp_rank = hcg.get_stage_id()

    # the results
    node_model_state = NodeModelState()

    for p in range(pp_rank, src_stage_num, pp_degree):
        cache_key = (sharding_rank, p)
        assert cache_key in state_cache
        tmp_node_model_state = state_cache[cache_key]
        del state_cache[cache_key]
        node_model_state.add_weights(tmp_node_model_state.model_weights)
        node_model_state.add_opts(tmp_node_model_state.opt_state)
        node_model_state.add_master_weights(tmp_node_model_state.master_weights)
        node_model_state.set_lr_scheduler(tmp_node_model_state.lr_scheduler)

    group = hcg.get_pipe_parallel_group()

    # all gather
    def filter_func(name):
        stage_id = pp_line_context.map_name_to_stage(name[0])
        assert stage_id < pp_degree
        return stage_id == pp_rank

    node_model_state.reshard(group, filter_func)

    def name_map_func(structure_name, p_name):
        map_name = pp_line_context.map_name(structure_name, p_name)
        return map_name

    node_model_state.map_name(name_map_func)

    def split_func(name):
        structure_name = name[0]
        return pp_line_context.map_name_to_stage(structure_name)

    return node_model_state.split_state(split_func)
