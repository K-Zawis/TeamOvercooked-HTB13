"""Execute actual run_once AST with broker doubles, not Redis durability evidence."""
import ast
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

class PendingRecovery(unittest.TestCase):
    def setUp(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'src/depth/worker.py').read_text().replace('lazy import ','import ').replace('lazy from ','from '))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='DepthWorker')
        method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='run_once')
        self.task=SimpleNamespace(log_id='0001',image_path='image.tif',y_start=0,y_end=100)
        ns=dict(TASK_STREAM='depth',DepthTask=SimpleNamespace(from_fields=Mock(return_value=self.task)),logger=Mock())
        exec(compile(ast.Module(body=[method],type_ignores=[]),'<actual-depth-run-once>','exec'),ns)
        self.call=ns['run_once']
        self.r=Mock()
        self.client=Mock(GROUP_DEPTH='g')
        self.worker=SimpleNamespace(redis=self.r,client=self.client,group='g',consumer_name='new',process_task=Mock(return_value={}))
        self.row=('1-0',{'data':'{}'})

    def test_own_pending_before_claim_or_new(self):
        self.r.xreadgroup.return_value=[('depth',[self.row])]
        self.assertTrue(self.call(self.worker))
        self.r.xautoclaim.assert_not_called()
        self.assertEqual(self.r.xreadgroup.call_args.args[2],{'depth':'0'})
        self.client.complete_depth_chunk.assert_called_once()

    def test_abandoned_claim_before_new_and_minimum_idle(self):
        self.r.xreadgroup.return_value=[]
        self.r.xautoclaim.return_value=['9-0',[self.row],[]]
        self.assertTrue(self.call(self.worker))
        self.assertEqual(self.r.xreadgroup.call_count,1)
        self.assertEqual(self.r.xautoclaim.call_args.kwargs,dict(min_idle_time=300000,start_id='0-0',count=1))
        self.assertEqual(self.worker._claim_cursor,'9-0')

    def test_redis62_bounded_cursor_and_new_fallback(self):
        self.r.xreadgroup.return_value=[]
        self.r.xautoclaim.side_effect=[['9-0',[]],['0-0',[]]]
        self.assertFalse(self.call(self.worker))
        self.assertFalse(self.call(self.worker))
        self.assertEqual([c.kwargs['start_id'] for c in self.r.xautoclaim.call_args_list],['0-0','9-0'])
        self.assertEqual([c.args[2] for c in self.r.xreadgroup.call_args_list],[{'depth':'0'},{'depth':'>'}]*2)

    def test_failed_claim_remains_pending_and_retries_own(self):
        self.r.xreadgroup.side_effect=[[],[('depth',[self.row])]]
        self.r.xautoclaim.return_value=['0-0',[self.row]]
        self.worker.process_task.side_effect=[OSError('crash'),{}]
        with self.assertRaises(OSError):self.call(self.worker)
        self.client.complete_depth_chunk.assert_not_called()
        self.assertTrue(self.call(self.worker))
        self.client.complete_depth_chunk.assert_called_once()

    def test_broker_claim_failure_not_swallowed(self):
        self.r.xreadgroup.return_value=[]
        self.r.xautoclaim.side_effect=OSError('offline')
        with self.assertRaises(OSError):self.call(self.worker)
        self.client.complete_depth_chunk.assert_not_called()

    def test_poison_pending_cursor_advances_then_allows_new(self):
        self.r.xreadgroup.side_effect=[[('depth',[self.row])],[],[('depth',[('2-0',{'data':'{}'})])]]
        self.r.xautoclaim.return_value=['0-0',[]]
        self.worker.process_task.side_effect=[ValueError('poison'),{}]
        with self.assertRaises(ValueError): self.call(self.worker)
        self.assertTrue(self.call(self.worker))
        self.assertEqual([c.args[2] for c in self.r.xreadgroup.call_args_list],
                         [{'depth':'0'},{'depth':'1-0'},{'depth':'>'}])

