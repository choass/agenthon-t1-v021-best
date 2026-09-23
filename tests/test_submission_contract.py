import json
import time
import unittest
from unittest.mock import patch
from t1_agent.config import Config
from t1_agent.client import ChatClient, BudgetUnavailable, ModelError
from t1_agent.workflow import Workflow, STATE_PREFIX

class ContractTests(unittest.TestCase):
    def test_admission_including_failed_attempt(self):
        c = ChatClient(Config('https://house.invalid', 'house', official=True, request_retries=0))
        c.usage.calls = 24
        with patch.object(c, '_exchange', return_value={'status': 503}):
            with self.assertRaises(ModelError):
                c.complete([], time.monotonic()+60)
        self.assertEqual(c.usage.calls, 25)
        with patch.object(c, '_exchange') as exchange:
            with self.assertRaises(BudgetUnavailable):
                c.complete([], time.monotonic()+60)
            exchange.assert_not_called()

    def test_house_protocol_and_diagnostic_tokens(self):
        c = ChatClient(Config('https://house.invalid', 'house', api_key='dummy-test-only', official=True))
        c.usage.input_tokens = 2_000_000
        c.usage.output_tokens = 80_000
        reply = {'data': {'usage': {'prompt_tokens': 10, 'completion_tokens': 2}, 'choices': [{'message': {'content':'ok'}, 'finish_reason':'stop'}]}}
        with patch.object(c, '_exchange', return_value=reply) as exchange:
            c.complete([], time.monotonic()+60)
            spec = exchange.call_args.args[0]
        self.assertEqual(spec['url'], 'https://house.invalid/v1/chat/completions')
        self.assertEqual(spec['headers']['Authorization'], 'Bearer dummy-test-only')
        self.assertEqual(spec['payload']['max_tokens'], 4000)
        self.assertEqual(spec['payload']['chat_template_kwargs'], {'enable_thinking':False})

    def test_state_has_request_budget(self):
        msg = Workflow().state_message(elapsed=1, limit=1800, steps=20, max_steps=250,
            input_used=2_000_000, input_cap=None, output_used=80_000, output_cap=4000,
            requests_used=24, request_cap=25)
        state = json.loads(msg['content'][len(STATE_PREFIX):])
        self.assertEqual(state['remaining']['requests'], 1)
        self.assertNotIn('output_tokens', state['remaining'])
        self.assertIn('Finalize now', state['directive'])

if __name__ == '__main__':
    unittest.main()
