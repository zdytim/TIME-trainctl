"""Contract checks for selected quota tiers in both client entry points."""
import unittest
from unittest.mock import patch
import timectl
import time_mcp
import time_core


class TierTests(unittest.TestCase):
    def test_cli_preserves_selected_tier(self):
        args=timectl.build_parser().parse_args(['validate','--tier','important','--','python','train.py'])
        self.assertEqual(timectl.make_job(args)['selected_tier'],'important')

    def test_mcp_preserves_selected_tier(self):
        args={'command':['python','train.py'],'selected_tier':'debug'}
        self.assertEqual(time_mcp.job_from_args(args)['selected_tier'],'debug')
        self.assertIn('selected_tier',time_mcp.job_properties())

    def test_mcp_rejects_invalid_tier(self):
        with self.assertRaises(Exception):
            time_mcp.job_from_args({'command':['python'],'selected_tier':'imaginary'})

    def test_month_wire_format(self):
        self.assertEqual(time_core.parse_month('202611'),'2026-11')
        self.assertEqual(time_core.parse_month('2026-11'),'2026-11')
        for bad in ('2026-13','202600','2026-1',None):
            with self.assertRaises(ValueError): time_core.parse_month(bad)

    def test_missing_tier_is_rejected(self):
        with self.assertRaises(ValueError): time_core.normalize_command_job(['python'])

    def test_mcp_month_request_uses_platform_wire_month(self):
        server=time_mcp.Server.__new__(time_mcp.Server)
        server.consent=object(); server.ledger=object(); server.client=object()
        observed={}
        def mutate(method,path,body,intent,timeout,request_id=None):
            observed.update(body)
            return {'application':{'id':'a'}},request_id,'confirmed'
        server.mutate=mutate
        caps={'capabilities':{'monthly_requests':{'available':True,'idempotency':'owner-request-id-v1'}}}
        with patch.object(time_mcp,'discover_server_capabilities',return_value=caps):
            server.call('time_create_request',{'kind':'monthly_gpu_hours','requested':{'gpu_hours':2,'month':'202611','client_request_id':'abc-12345'},
                        'reason':'need hours','confirm':True,'user_intent':'create_request'})
        self.assertEqual(observed['requested']['month'],'2026-11')
        self.assertEqual(observed['requested']['client_request_id'],'abc-12345')

    def test_no_consent_hides_mutation_tools(self):
        server=time_mcp.Server.__new__(time_mcp.Server); server.consent=None
        names={tool['name'] for tool in server.tools()}
        self.assertIn('time_validate_job',names)
        self.assertNotIn('time_submit_job',names)
        self.assertNotIn('time_cancel_job',names)

if __name__=='__main__': unittest.main()
