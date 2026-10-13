"""Local protocol fixture. No network, credentials, or vendor code execution."""
import json
import sys

for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method = request['method']
    if method == 'initialize':
        result = {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'local-fixture','version':'1'}}
    elif method == 'tools/list':
        cursor = request.get('params',{}).get('cursor')
        name = 'second' if cursor else 'first'
        result = {'tools':[{'name':name,'description':'offline fixture','inputSchema':{'type':'object'}}]}
        if not cursor:
            result['nextCursor'] = 'page2'
    elif method == 'tools/call':
        result = {'content':[{'type':'text','text':json.dumps({'status':200,'result':{'code':'OK','data':{'items':[{'asin':'LOCAL','monthly_sales':10}],'total':1}}})}]}
    elif method == 'ping':
        result = {}
    else:
        print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Unsupported fixture method'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result}), flush=True)
