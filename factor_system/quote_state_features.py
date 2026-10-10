"""Vectorized descriptive quote features, with separate missing-book masks.

These are snapshot state features, not order flow, matched volume, queue priority
or an executable fill model. Arrays and derived vectors stay in native storage.
"""
from __future__ import annotations
import numpy as np


def quote_state_features(ask_prices, bid_prices, ask_sizes, bid_sizes):
    inputs=[np.asarray(x) for x in (ask_prices,bid_prices,ask_sizes,bid_sizes)]
    if any(x.ndim!=2 or x.shape[1]!=5 or x.dtype!=np.dtype('float64') for x in inputs):
        raise ValueError('quote_features_require_float64_five_levels')
    if len({x.shape for x in inputs})!=1:
        raise ValueError('quote_feature_shapes_differ')
    asks,bids,av,bv=inputs
    n=len(asks)
    best_finite=np.isfinite(np.column_stack([asks[:,0],bids[:,0],av[:,0],bv[:,0]])).all(axis=1)
    best_positive=(asks[:,0]>0)&(bids[:,0]>0)&(av[:,0]>0)&(bv[:,0]>0)
    price_valid=np.isfinite(asks[:,0])&np.isfinite(bids[:,0])&(asks[:,0]>0)&(bids[:,0]>0)&(asks[:,0]>=bids[:,0])
    best_valid=best_finite&best_positive&price_valid
    complete=np.logical_and.reduce([np.isfinite(x).all(axis=1)&(x>0).all(axis=1) for x in inputs])
    ordered=(np.diff(asks,axis=1)>=0).all(axis=1)&(np.diff(bids,axis=1)<=0).all(axis=1)
    depth_valid=complete&ordered&best_valid
    spread=np.full(n,np.nan,dtype='float64')
    top_imbalance=spread.copy();depth_imbalance=spread.copy()
    with np.errstate(invalid='ignore',over='ignore',divide='ignore'):
        # Scale before arithmetic to avoid overflow and keep units cancellable.
        price_scale=np.maximum(asks[:,0],bids[:,0])
        ar=asks[:,0]/price_scale;br=bids[:,0]/price_scale
        spread_values=2*(ar-br)/(ar+br)
        qty_scale=np.maximum(av[:,0],bv[:,0])
        aq=av[:,0]/qty_scale;bq=bv[:,0]/qty_scale
        top_values=(bq-aq)/(bq+aq)
        scale=np.maximum(av.max(axis=1),bv.max(axis=1))
        at=(av/scale[:,None]).sum(axis=1);bt=(bv/scale[:,None]).sum(axis=1)
        depth_values=(bt-at)/(bt+at)
    spread_valid=price_valid&np.isfinite(spread_values)
    top_valid=best_valid&np.isfinite(top_values)
    depth_valid=depth_valid&np.isfinite(depth_values)
    spread[spread_valid]=spread_values[spread_valid]
    top_imbalance[top_valid]=top_values[top_valid]
    depth_imbalance[depth_valid]=depth_values[depth_valid]
    return {'quoted_spread_fraction':spread,'best_depth_imbalance':top_imbalance,
            'five_depth_imbalance':depth_imbalance,'spread_valid':spread_valid,
            'best_imbalance_valid':top_valid,'five_imbalance_valid':depth_valid}
