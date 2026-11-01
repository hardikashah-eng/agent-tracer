from backend.causal import provenance, experiment
import pytest

def test_provenance_versions():
    events=[{'seq':1,'kind':'context.snapshot','name':'model-a','payload':{'messages':[{'id':'x','role':'user','content':'hello','estimated_tokens':2}]}},{'seq':2,'kind':'context.snapshot','name':'model-b','payload':{'messages':[{'id':'x','role':'user','content':'changed','estimated_tokens':2}]}}]
    g=provenance(events)
    assert len(g['items'])==2 and len(g['edges'])==2

def test_ablation_is_reproducible():
    snap={'seq':3,'payload':{'messages':[{'id':'a','role':'tool','content':'traceback timeout'}, {'id':'b','role':'user','content':'fix it'}]}}
    a=experiment(snap,['a'],30,7)
    assert a==experiment(snap,['a'],30,7)
    assert a['delta']>0
    with pytest.raises(ValueError): experiment(snap,['missing'])
