from backend.efficiency import analyze

def test_large_tool_and_removed_context():
    events = [
      {'seq':1,'kind':'context.snapshot','name':'first','payload':{'estimated_tokens':4,'messages':[{'id':'u','category':'conversation','content':'hello','estimated_tokens':4}]}},
      {'seq':2,'kind':'tool.after','name':'search','payload':{}},
      {'seq':3,'kind':'context.snapshot','name':'second','payload':{'estimated_tokens':130,'messages':[{'id':'t','category':'tool','source':'search','content':'x'*500,'estimated_tokens':130}]}},
    ]
    result=analyze(events,large_tool_tokens=100)
    assert result['summary']['peak_prompt_estimated_tokens']==130
    assert {'large_tool_output','context_removed'} <= {f['kind'] for f in result['findings']}
    assert 'unused_tool_result' not in {f['kind'] for f in result['findings']}
