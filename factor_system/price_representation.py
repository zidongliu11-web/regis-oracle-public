"""Fixed representation diagnostics; results never certify a price basis.

Original float64 values and strict masks remain unchanged. A common float32
comparison is a hypothesis check, not a replacement tolerance or data correction.
"""
from __future__ import annotations
import numpy as np
from .quote_quality import ohlc_quality

SCALES=tuple((f'1e{k:+d}',10.**k) for k in range(-4,5))


def _arrays(values):
    arrays=[np.asarray(x) for x in values]
    if any(x.ndim!=1 or x.dtype!=np.dtype('float64') for x in arrays) or len({x.shape for x in arrays})!=1:
        raise ValueError('representation_requires_equal_float64_vectors')
    return arrays


def ohlc_representation_summary(opening,high,low,close):
    raw=_arrays((opening,high,low,close));strict=ohlc_quality(*raw)
    with np.errstate(over='ignore',invalid='ignore'):
        common=[x.astype('float32') for x in raw]
    valid=np.logical_and.reduce([np.isfinite(x)&(x>0) for x in common])
    o,h,l,c=common;ordered=valid&(l<=np.minimum(o,c))&(np.maximum(o,c)<=h)
    return {'rows':len(o),'original_malformed':int(strict['malformed'].sum()),'original_beyond8float64':int(strict['beyond_roundoff'].sum()),
            'common_float32_finite_positive':int(valid.sum()),'common_float32_ordered':int(ordered.sum()),
            'original_bad_common32_ordered':int((strict['beyond_roundoff']&ordered).sum()),
            'exact_float32_images':{key:int((np.isfinite(x)&np.isfinite(y)&(x>0)&(x==y.astype('float64'))).sum()) for key,x,y in zip(['open','high','low','close'],raw,common)},
            'source_or_model_admitted':False}


def paired_price_representation(opening,high,low,close,window,candidate=None):
    window=_arrays((window,))[0]
    if ((~np.isfinite(window))|(window<=0)).any() or not all(np.isfinite(x) and x>0 for x in [opening,high,low,close]):
        raise ValueError('representation_malformed_prices')
    if candidate is not None and (not np.isfinite(candidate) or candidate<=0):
        raise ValueError('representation_malformed_candidate')
    result={}
    with np.errstate(over='ignore',invalid='ignore'):
        c32=np.float32(close);h32=np.float32(high);l32=np.float32(low);o32=np.float32(opening)
        for label,scale in SCALES:
            values=window*scale;converted=values.astype('float32')
            valid=np.isfinite(converted)&(converted>0)
            pre=np.float32(candidate*scale) if candidate is not None else np.float32(np.nan)
            result[label]={'raw64_close_match':bool((values==close).any()),
               'raw64_open_preopen_match':bool(candidate is not None and candidate*scale==opening),
               'common32_close_match':bool(np.isfinite(c32) and (valid&(converted==c32)).any()),
               'common32_observed_outside_HL':bool(np.isfinite(h32) and np.isfinite(l32) and (valid&((converted<l32)|(converted>h32))).any()),
               'common32_open_preopen_match':bool(np.isfinite(o32) and np.isfinite(pre) and pre>0 and pre==o32)}
    return result
