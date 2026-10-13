import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from research_workflow.connectors.sellersprite import _structured, SellerSpriteConnector
from research_workflow.connectors.base import ConnectorError, with_retries
from scripts import run_research as cli
from test_cli_live import REQUIRED_TOOLS


def test_mcp_text_content_and_errors_are_parsed():
    payload = {'data': {'items': [{'asin':'A'}], 'total':1}}
    assert _structured({'content':[{'type':'text','text':json.dumps(payload)}]}) == payload
    obj = SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(payload))])
    assert _structured(obj) == payload
    with pytest.raises(ConnectorError):
        _structured({'isError':True,'content':[{'type':'text','text':'sensitive failure'}]})


def test_live_month_is_validated_before_gateway_or_file_write(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, '_build_gateway', lambda _: pytest.fail('gateway must not be built'))
    args = cli.build_parser().parse_args(['ingest','--project','p','--live','--months','../../outside'])
    with pytest.raises(ValueError):
        asyncio.run(cli._fetch_live(args, tmp_path))


def test_login_awaits_validation_and_emits_one_json(tmp_path, monkeypatch, capsys):
    calls=[]
    class Gateway:
        async def list_tools(self, server):
            calls.append('validate')
            return sorted(REQUIRED_TOOLS)
        async def call_tool(self, *args):
            calls.append('login')
            return {'structured_content':{'ready':True}}
    monkeypatch.setattr(cli, '_build_gateway', lambda _: Gateway())
    cli.main(['login','--wait','0'])
    assert json.loads(capsys.readouterr().out)['login'] == 'completed'
    assert calls == ['validate','login']


def test_truncated_pagination_cannot_be_reported_complete():
    class Gateway:
        async def call_tool(self, *args):
            return {'structured_content':{'items':[{'asin':'A'}],'total':10}}
    with pytest.raises(ConnectorError, match='pag'):
        asyncio.run(SellerSpriteConnector(Gateway()).product_research(marketplace='US',page_size=1,max_pages=1))


def test_retry_failure_does_not_echo_external_secret():
    async def fail():
        raise RuntimeError('Bearer DO_NOT_ECHO')
    with pytest.raises(ConnectorError) as exc:
        asyncio.run(with_retries(fail, attempts=1))
    assert 'DO_NOT_ECHO' not in str(exc.value)


def test_upstream_nested_result_and_http_failure():
    from research_workflow.connectors.sellersprite import _extract_rows_and_total
    payload = {'status':200, 'result':{'code':'OK','data':{'items':[{'asin':'A'}],'total':1}}}
    assert _extract_rows_and_total(payload) == ([{'asin':'A'}], 1)
    with pytest.raises(ConnectorError):
        _extract_rows_and_total({'status':403, 'result':{'code':'NOT_AUTHORIZED','data':{'items':[]}}})


def test_installed_sdk_stdio_transport_with_local_fixture():
    """Real installed SDK/stdio; supplier data is synthetic, no vendor acceptance."""
    import sys
    from research_workflow.mcp import MCPGateway, MCPRegistry
    from research_workflow.mcp.config import MCPServerConfig
    from research_workflow.connectors.sellersprite import _extract_rows_and_total
    gateway = MCPGateway(MCPRegistry({'local':MCPServerConfig(
        name='local',transport='stdio',command=sys.executable,
        args=(str(Path(__file__).parent / 'fixtures' / 'mcp_stub.py'),))}))
    async def check():
        assert await gateway.list_tools('local') == ['first','second']
        response = await gateway.call_tool('local','first',{})
        assert _extract_rows_and_total(_structured(response)) == ([{'asin':'LOCAL','monthly_sales':10}],1)
    asyncio.run(check())


def test_reordered_overlapping_pages_are_incomplete():
    class Gateway:
        async def call_tool(self, server, tool, arguments):
            rows = [{'asin':'A'},{'asin':'B'}]
            if arguments['params']['page'] == 2:
                rows.reverse()
            return {'structured_content':{'items':rows,'total':4}}
    with pytest.raises(ConnectorError):
        asyncio.run(SellerSpriteConnector(Gateway()).product_research(marketplace='US',page_size=2,max_pages=2))


def test_nested_result_list_has_controlled_handling():
    from research_workflow.connectors.sellersprite import _extract_rows_and_total
    assert _extract_rows_and_total({'data':{'status':200,'result':[{'asin':'A'}]}})[0] == [{'asin':'A'}]
