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

    RGCN layer implementation.
"""

import torch as th
from torch import nn
import torch.nn.functional as F
import dgl.nn as dglnn

from .ngnn_mlp import NGNNMLP
from .gnn_encoder_base import GraphConvEncoder


class RelGraphConvLayer(nn.Module):
    r"""Relational graph convolution layer.

    Parameters
    ----------
    in_feat : int
        Input feature size.
    out_feat : int
        Output feature size.
    rel_names : list[str]
        Relation names.
    num_bases : int
        Number of bases. If is none, use number of relations. Default: None.
    weight : bool, optional
        True if a linear layer is applied after message passing. Default: True
    bias : bool, optional
        True if bias is added. Default: True
    activation : callable, optional
        Activation function. Default: None
    self_loop : bool, optional
        True to include self loop message. Default: False
    dropout : float, optional
        Dropout rate. Default: 0.0
    num_ffn_layers_in_gnn: int, optional
        Number of layers of ngnn between gnn layers
    ffn_actication: torch.nn.functional
        Activation Method for ngnn
    norm : str, optional
        Normalization Method. Default: None
    """
    def __init__(self,
                 in_feat,
                 out_feat,
                 rel_names,
                 num_bases,
                 *,
                 weight=True,
                 bias=True,
                 activation=None,
                 self_loop=False,
                 dropout=0.0,
                 num_ffn_layers_in_gnn=0,
                 ffn_activation=F.relu,
                 norm=None):
        super(RelGraphConvLayer, self).__init__()
        self.in_feat = in_feat
        self.out_feat = out_feat
        self.rel_names = rel_names
        self.num_bases = num_bases
        self.bias = bias
        self.activation = activation
        self.self_loop = self_loop

        self.conv = dglnn.HeteroGraphConv({
                rel : dglnn.GraphConv(in_feat, out_feat, norm='right', weight=False, bias=False)
                for rel in rel_names
            })

        self.use_weight = weight
        self.use_basis = num_bases < len(self.rel_names) and weight
        if self.use_weight:
            if self.use_basis:
                self.basis = dglnn.WeightBasis(
                    (in_feat, out_feat), num_bases, len(self.rel_names))
            else:
                self.weight = nn.Parameter(th.Tensor(len(self.rel_names), in_feat, out_feat))
                nn.init.xavier_uniform_(self.weight, gain=nn.init.calculate_gain('relu'))

        # get the node types
        ntypes = set()
        for rel in rel_names:
            ntypes.add(rel[0])
            ntypes.add(rel[2])

        # normalization
        self.norm = None
        if activation is None and norm is not None:
            raise ValueError("Cannot set gnn norm layer when activation layer is None")
        if norm == "batch":
            self.norm = nn.ParameterDict({ntype:nn.BatchNorm1d(out_feat) for ntype in ntypes})
        elif norm == "layer":
            self.norm = nn.ParameterDict({ntype:nn.LayerNorm(out_feat) for ntype in ntypes})
        else:
            # by default we don't apply any normalization
            self.norm = None

        # bias
        if bias:
            self.h_bias = nn.Parameter(th.Tensor(out_feat))
            nn.init.zeros_(self.h_bias)

        # weight for self loop
        if self.self_loop:
            self.loop_weight = nn.Parameter(th.Tensor(in_feat, out_feat))
            nn.init.xavier_uniform_(self.loop_weight,
                                    gain=nn.init.calculate_gain('relu'))

        # ngnn
        self.num_ffn_layers_in_gnn = num_ffn_layers_in_gnn
        self.ngnn_mlp = NGNNMLP(out_feat, out_feat,
                                     num_ffn_layers_in_gnn, ffn_activation, dropout)

        self.dropout = nn.Dropout(dropout)

    # pylint: disable=invalid-name
    def forward(self, g, inputs):
        """Forward computation

        Parameters
        ----------
        g : DGLHeteroGraph
            Input graph.
        inputs : dict[str, torch.Tensor]
            Node feature for each node type.
        Returns
        -------
        dict[str, torch.Tensor]
            New node features for each node type.
        """
        g = g.local_var()
        if self.use_weight:
            weight = self.basis() if self.use_basis else self.weight
            wdict = {self.rel_names[i] : {'weight' : w.squeeze(0)} \
                for i, w in enumerate(th.split(weight, 1, dim=0))}
        else:
            wdict = {}

        if g.is_block:
            inputs_src = inputs
            # DGL's message passing module requires to access the destination node embeddings.
            inputs_dst = {}
            for k in g.dsttypes:
                # If the destination node type exists in the input embeddings,
                # we can get from the input node embeddings directly because
                # the input nodes of DGL's block also contain the destination nodes
                if k in inputs:
                    inputs_dst[k] = inputs[k][:g.number_of_dst_nodes(k)]
                else:
                    # If the destination node type doesn't exist (this may happen if
                    # we use RGCN to construct node features), we should create a zero
                    # tensor. This tensor won't be used for computing embeddings.
                    # We need this just to fulfill the requirements of DGL message passing
                    # modules.
                    assert not self.self_loop, \
                            f"We cannot allow self-loop if node {k} doesn't have input features."
                    inputs_dst[k] = th.zeros((g.num_dst_nodes(k), self.in_feat),
                                             dtype=th.float32, device=g.device)
        else:
            inputs_src = inputs_dst = inputs

        hs = self.conv(g, (inputs_src, inputs_dst), mod_kwargs=wdict)

        def _apply(ntype, h):
            if self.self_loop:
                h = h + th.matmul(inputs_dst[ntype], self.loop_weight)
            if self.bias:
                h = h + self.h_bias
            if self.norm:
                h = self.norm[ntype](h)
            if self.activation:
                h = self.activation(h)
            if self.num_ffn_layers_in_gnn > 0:
                h = self.ngnn_mlp(h)
            return self.dropout(h)

        for k, _ in inputs.items():
            if g.number_of_dst_nodes(k) > 0:
                if k not in hs:
                    hs[k] = inputs[k][0:g.number_of_dst_nodes(k)]
                    # TODO the above might fail if the device is a different GPU
        return {ntype : _apply(ntype, h) for ntype, h in hs.items()}


class RelationalGCNEncoder(GraphConvEncoder):
    r""" Relational graph conv encoder.

    Parameters
    ----------
    g : DistGraph
        The distributed graph object.
    h_dim : int
        Hidden dimension
    out_dim : int
        Output dimension
    num_bases: int
        Number of bases.
    num_hidden_layers : int
        Number of hidden layers. Total GNN layers is equal to num_hidden_layers + 1. Default 1
    dropout : float
        Dropout. Default 0.
    use_self_loop : bool
        Whether to add selfloop. Default True
    last_layer_act : torch.function
        Activation for the last layer. Default None
    num_ffn_layers_in_gnn: int
        Number of ngnn gnn layers between GNN layers
    norm : str, optional
        Normalization Method. Default: None
    """
    def __init__(self,
                 g,
                 h_dim, out_dim,
                 num_bases=-1,
                 num_hidden_layers=1,
                 dropout=0,
                 use_self_loop=True,
                 last_layer_act=False,
                 num_ffn_layers_in_gnn=0,
                 norm=None):
        super(RelationalGCNEncoder, self).__init__(h_dim, out_dim, num_hidden_layers)
        if num_bases < 0 or num_bases > len(g.canonical_etypes):
            self.num_bases = len(g.canonical_etypes)
        else:
            self.num_bases = num_bases

        # h2h
        for _ in range(num_hidden_layers):
            self.layers.append(RelGraphConvLayer(
                h_dim, h_dim, g.canonical_etypes,
                self.num_bases, activation=F.relu, self_loop=use_self_loop,
                dropout=dropout, num_ffn_layers_in_gnn=num_ffn_layers_in_gnn,
                ffn_activation=F.relu, norm=norm))
        # h2o
        self.layers.append(RelGraphConvLayer(
            h_dim, out_dim, g.canonical_etypes,
            self.num_bases, activation=F.relu if last_layer_act else None,
            self_loop=use_self_loop, norm=norm if last_layer_act else None))

    # TODO(zhengda) refactor this to support edge features.
    def forward(self, blocks, h):
        """Forward computation

        Parameters
        ----------
        blocks: DGL MFGs
            Sampled subgraph in DGL MFG
        h: dict[str, torch.Tensor]
            Input node feature for each node type.
        """
        for layer, block in zip(self.layers, blocks):
            h = layer(block, h)
        return h
