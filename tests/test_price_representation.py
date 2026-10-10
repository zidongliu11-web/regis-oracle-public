import numpy as np
import pytest
from factor_system.price_representation import ohlc_representation_summary as summary,paired_price_representation as paired


def f(x):return np.array(x,dtype='float64')


def test_mixed_precision_order_discrepancy_is_diagnostic_only():
    high=float(np.float32(10.2));o=f([10.2]);h=f([high]);l=f([10.]);c=f([10.05])
    before=[x.copy() for x in [o,h,l,c]];r=summary(o,h,l,c)
    assert r['original_beyond8float64']==1 and r['original_bad_common32_ordered']==1
    assert not r['source_or_model_admitted']
    for a,b in zip([o,h,l,c],before):np.testing.assert_array_equal(a,b)


def test_material_order_error_not_explained_by_float32():
    r=summary(f([12.]),f([11.]),f([10.]),f([10.5]))
    assert r['original_beyond8float64']==1 and r['original_bad_common32_ordered']==0


def test_field_image_counts_are_exact_not_tolerance_based():
    r=summary(f([10.1]),f([float(np.float32(10.2))]),f([10.]),f([float(np.float32(10.05))]))
    assert r['exact_float32_images']=={'open':0,'high':1,'low':1,'close':1}


def test_common_precision_price_pair_does_not_change_raw_match():
    r=paired(10.,11.,10.,float(np.float32(10.1)),f([10.1]),10.)
    assert not r['1e+0']['raw64_close_match'] and r['1e+0']['common32_close_match']
    assert r['1e+0']['common32_open_preopen_match']


def test_raw_open_comparison_distinguishes_common32_aliases():
    a=paired(10.1,11.,10.,10.5,f([10.5]),10.1)
    b=paired(10.1,11.,10.,10.5,f([10.5]),float(np.nextafter(10.1,np.inf)))
    assert a['1e+0']['raw64_open_preopen_match']
    assert not b['1e+0']['raw64_open_preopen_match']
    assert a['1e+0']['common32_open_preopen_match'] and b['1e+0']['common32_open_preopen_match']


def test_scale_hypotheses_predefined_and_separate():
    r=paired(10.,11.,10.,10.1,f([101.]),100.)
    assert len(r)==9 and r['1e-1']['common32_close_match']
    assert not r['1e+0']['common32_close_match']


def test_overflow_does_not_match_infinities():
    r=paired(1e300,1e301,1e299,1e300,f([1e300]),1e300)
    assert not any(x['common32_close_match'] or x['common32_open_preopen_match'] for x in r.values())
    s=summary(f([1e300]),f([1e301]),f([1e299]),f([1e300]))
    assert s['common_float32_finite_positive']==0


def test_empty_window_not_fabricated():
    r=paired(10.,11.,9.,10.,f([]))
    assert not any(x['raw64_close_match'] or x['common32_close_match'] for x in r.values())


@pytest.mark.parametrize('values',[np.array([10.],dtype='float32'),np.array([[10.]],dtype='float64')])
def test_wrong_dtype_or_shape_denied(values):
    with pytest.raises(ValueError):summary(values,f([11.]),f([9.]),f([10.]))


def test_nonfinite_window_denied():
    with pytest.raises(ValueError):paired(10.,11.,9.,10.,f([np.nan]))
