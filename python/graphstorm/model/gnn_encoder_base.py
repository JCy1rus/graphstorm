"""
    Copyright 2023 Contributors

    Licensed under the Apache License, Version 2.0 (the "License");
    you may not use this file except in compliance with the License.
    You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

    Unless required by applicable law or agreed to in writing, software
    distributed under the License is distributed on an "AS IS" BASIS,
    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
    See the License for the specific language governing permissions and
    limitations under the License.

    Relational GNN
"""

from functools import partial
import logging

import dgl
import torch as th
from torch import nn
from dgl.distributed import DistTensor, node_split
from .gs_layer import GSLayer

from ..utils import get_rank, barrier

class GraphConvEncoder(GSLayer):     # pylint: disable=abstract-method
    r"""General encoder for graph data.

    Parameters
    ----------
    h_dim : int
        Hidden dimension
    out_dim : int
        Output dimension
    num_hidden_layers : int
        Number of hidden layers. Total GNN layers is equal to num_hidden_layers + 1. Default 1
    """
    def __init__(self,
                 h_dim,
                 out_dim,
                 num_hidden_layers=1):
        super(GraphConvEncoder, self).__init__()
        self._h_dim = h_dim
        self._out_dim = out_dim
        self._num_hidden_layers = num_hidden_layers
        self._layers = nn.ModuleList()  # GNN layers.

    @property
    def in_dims(self):
        return self._h_dim

    @property
    def out_dims(self):
        return self._out_dim

    @property
    def h_dims(self):
        """ The hidden dimension size.
        """
        return self._h_dim

    @property
    def num_layers(self):
        """ The number of GNN layers.
        """
        # The number of GNN layer is the number of hidden layers + 1
        return self._num_hidden_layers + 1

    @property
    def layers(self):
        """ GNN layers
        """
        return self._layers

    def dist_inference(self, g, get_input_embeds, batch_size, fanout,
                       edge_mask=None, task_tracker=None):
        """Distributed inference of final representation over all node types.

        Parameters
        ----------
        g : DistGraph
            The distributed graph.
        gnn_encoder : GraphConvEncoder
            The GNN encoder on the graph.
        get_input_embeds : callable
            Get the node features of the input nodes.
        batch_size : int
            The batch size for the GNN inference.
        fanout : list of int
            The fanout for computing the GNN embeddings in a GNN layer.
        edge_mask : str
            The edge mask indicates which edges are used to compute GNN embeddings.
        task_tracker : GSTaskTrackerAbc
            The task tracker.

        Returns
        -------
        dict of Tensor : the final GNN embeddings of all nodes.
        """
        return dist_inference(g, self, get_input_embeds, batch_size, fanout,
                              edge_mask=edge_mask, task_tracker=task_tracker)

def dist_inference_one_layer(layer_id, g, dataloader, target_ntypes, layer, get_input_embeds,
                             device, task_tracker):
    """ Run distributed inference for one GNN layer.

    Parameters
    ----------
    layer_id : str
        The layer ID.
    g : DistGraph
        The full distributed graph.
    target_ntypes : list of str
        The node types where we compute GNN embeddings.
    dataloader : Pytorch dataloader
        The iterator over the nodes for computing GNN embeddings.
    layer : nn module
        A GNN layer
    get_input_embeds : callable
        Get the node features.
    device : Pytorch device
        The device to run mini-batch computation.
    task_tracker : GSTaskTrackerAbc
        The task tracker.

    Returns
    -------
    dict of Tensors : the inferenced tensors.
    """
    y = {}
    for iter_l, (input_nodes, output_nodes, blocks) in enumerate(dataloader):
        if iter_l % 100000 == 0 and get_rank() == 0:
            logging.info("[Rank 0] dist_inference: finishes %d iterations.", iter_l)

        if task_tracker is not None:
            task_tracker.keep_alive(report_step=iter_l)
        block = blocks[0].to(device)
        if not isinstance(input_nodes, dict):
            # This happens on a homogeneous graph.
            assert len(g.ntypes) == 1
            input_nodes = {g.ntypes[0]: input_nodes}

        if not isinstance(output_nodes, dict):
            # This happens on a homogeneous graph.
            assert len(g.ntypes) == 1
            output_nodes = {g.ntypes[0]: output_nodes}

        h = get_input_embeds(input_nodes)
        h = layer(block, h)

        # For the first iteration, we need to create output tensors.
        if iter_l == 0:
            # Infer the hidden dim size.
            # Here we assume all node embeddings have the same dim size.
            h_dim = 0
            dtype = None
            for k in h:
                assert len(h[k].shape) == 2, \
                        "The embedding tensors should have only two dimensions."
                h_dim = h[k].shape[1]
                dtype = h[k].dtype
            assert h_dim > 0, "Cannot inference the hidden dim size."

            # Create distributed tensors to store the embeddings.
            for k in target_ntypes:
                y[k] = DistTensor((g.number_of_nodes(k), h_dim),
                                  dtype=dtype, name=f'h-{layer_id}',
                                  part_policy=g.get_node_partition_policy(k),
                                  # TODO(zhengda) this makes the tensor persistent in memory.
                                  persistent=True)

        for k in h.keys():
            # some ntypes might be in the tensor h but are not in the output nodes
            # that have empty tensors
            if k in output_nodes:
                assert k in y, "All mini-batch outputs should have the same tensor names."
                y[k][output_nodes[k]] = h[k].cpu()
    return y

def dist_inference(g, gnn_encoder, get_input_embeds, batch_size, fanout,
                   edge_mask=None, task_tracker=None):
    """Distributed inference of final representation over all node types.

    Parameters
    ----------
    g : DistGraph
        The distributed graph.
    gnn_encoder : GraphConvEncoder
        The GNN encoder on the graph.
    get_input_embeds : callable
        Get the node features.
    batch_size : int
        The batch size for the GNN inference.
    fanout : list of int
        The fanout for computing the GNN embeddings in a GNN layer.
    edge_mask : str
        The edge mask indicates which edges are used to compute GNN embeddings.
    task_tracker : GSTaskTrackerAbc
        The task tracker.

    Returns
    -------
    dict of Tensor : the final GNN embeddings of all nodes.
    """
    device = gnn_encoder.device
    with th.no_grad():
        next_layer_input = None
        for i, layer in enumerate(gnn_encoder.layers):
            infer_nodes = {}
            for ntype in g.ntypes:
                infer_nodes[ntype] = node_split(th.ones((g.number_of_nodes(ntype),),
                                                        dtype=th.bool),
                                                partition_book=g.get_partition_book(),
                                                ntype=ntype, force_even=False)
            # need to provide the fanout as a list, the number of layers is one obviously here
            fanout_i = [-1] if fanout is None or len(fanout) == 0 else [fanout[i]]
            sampler = dgl.dataloading.MultiLayerNeighborSampler(fanout_i, mask=edge_mask)
            dataloader = dgl.dataloading.DistNodeDataLoader(g, infer_nodes, sampler,
                                                            batch_size=batch_size,
                                                            shuffle=False,
                                                            drop_last=False)

            if i > 0:
                def get_input_embeds1(input_nodes, node_feats):
                    return {k: node_feats[k][input_nodes[k]].to(device) for k in input_nodes.keys()}
                get_input_embeds = partial(get_input_embeds1, node_feats=next_layer_input)
            next_layer_input = dist_inference_one_layer(str(i), g, dataloader,
                                                        list(infer_nodes.keys()),
                                                        layer, get_input_embeds, device,
                                                        task_tracker)
            barrier()
    return next_layer_input
