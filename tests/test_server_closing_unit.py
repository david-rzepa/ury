"""Server hook + exact pinned ERPNext query/builder; database boundaries are fakes.

Exercises invoice references delivered to native consolidation, not real SI/GL
posting. Fresh native integration must still prove merge logs, SI and GL entries.
"""
import ast
import copy
import datetime as dt
import hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / 'tests/fixtures/erpnext_pos_closing_entry_4cea6a7.py'
HOOK = ROOT / 'ury/ury/hooks/ury_pos_closing_entry.py'

class Refused(Exception):
    pass

class Doc(dict):
    def __getattr__(self, name):
        return self.get(name)
    def __setattr__(self, name, value):
        self[name] = value
    def set_status(self, *, update, status):
        self.status=status
    def set(self, name, value):
        self[name] = value
    def as_dict(self):
        return self
    def check_permission(self, permission):
        if self.get('denied'):
            raise Refused('Native read permission denied')
    def get_doc_before_save(self):
        return self.get('previous')

def load_functions(path, names, namespace):
    nodes = [n for n in ast.parse(path.read_text()).body
             if isinstance(n, ast.FunctionDef) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), 'exec'), namespace)

class ClosingTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime(2026, 10, 20, 14)
        self.user = 'staff@example.invalid'
        self.opening = Doc(name='OPEN-1', user=self.user, docstatus=1, status='Open',
            pos_profile='Origen', company='Origen', period_start_date='2026-10-20 10:00:00',
            balance_details=[Doc(mode_of_payment='Clip', opening_amount=0)])
        self.profile = Doc(name='Origen', company='Origen', branch='Origen')
        self.invoices = {}
        for name in ('STG00002', 'STG00003'):
            self.invoices[name] = Doc(name=name, owner=self.user, cashier=self.user,
                docstatus=1, pos_profile='Origen', company='Origen', branch='Origen',
                consolidated_invoice=None, posting_date='2026-10-20', posting_time='13:00:00',
                grand_total=25, net_total=20, total_qty=1, customer='Walk In',
                taxes=[Doc(account_head='VAT', rate=25, tax_amount=5)],
                payments=[Doc(mode_of_payment='Clip', amount=25)])
        self.doc = Doc(name='CLOSE-1', docstatus=0, user=self.user, pos_profile='Origen',
            company='Origen', pos_opening_entry='OPEN-1', period_start_date='1900-01-01',
            period_end_date='2100-01-01', grand_total=9000, net_total=8000, total_quantity=100,
            pos_transactions=[], taxes=[], payment_reconciliation=[Doc(mode_of_payment='Clip',
            closing_amount=50, custom_closing_amount=0, expected_amount=9000)])
        self.locks = []
        self.query_count = 0
        self.existing_closings = []
        self.after_lock = None
        self.frappe = SimpleNamespace(db=SimpleNamespace(sql=self.sql),
            utils=SimpleNamespace(get_datetime=self.datetime), _dict=Doc,
            new_doc=lambda kind: Doc(), get_doc=self.get_doc,
            has_permission=self.has_permission, session=SimpleNamespace(user=self.user),
            whitelist=lambda: lambda f:f, throw=self.refuse)
        self.namespace = {'frappe':self.frappe, 'get_datetime':self.datetime, 'flt':float,
                          'isfinite':__import__('math').isfinite}
        load_functions(NATIVE, ['get_pos_invoices', 'make_closing_entry_from_opening'], self.namespace)
        load_functions(HOOK, ['populate_server_closing', 'build_server_closing'], self.namespace)
        self.populate = self.namespace['populate_server_closing']
    def datetime(self, value=None):
        return self.now if value is None else dt.datetime.fromisoformat(str(value))
    def refuse(self, message):
        raise Refused(message)
    def has_permission(self, *args, **kwargs):
        return True
    def get_doc(self, kind, name, for_update=False):
        if for_update:
            self.locks.append((kind,(name,)))
            if kind == 'POS Invoice' and self.after_lock:
                callback,self.after_lock=self.after_lock,None
                callback()
        if kind == 'POS Opening Entry':
            if name != self.opening.name:
                raise Refused('Missing opening')
            return self.opening
        if kind == 'POS Profile':
            return self.profile
        return self.invoices[name]
    def sql(self, query, args, **kwargs):
        if '`tabPOS Closing Entry`' in query:
            self.assertIn('FOR UPDATE',query)
            opening,name=args
            return [(d.name,) for d in self.existing_closings if d.pos_opening_entry==opening
                    and d.name!=name and d.docstatus in (0,1)]
        self.query_count += 1
        if len(args)==2:
            user,profile=args
            start,end=None,None
        else:
            self.assertIn('FOR UPDATE',query)
            user,profile,start,end=args
        return [Doc(name=d.name,timestamp=str(d.posting_date)+' '+str(d.posting_time))
                for d in self.invoices.values() if d.owner==user and d.pos_profile==profile
                and d.docstatus==1 and not d.consolidated_invoice
                and (start is None or self.datetime(start)<=self.datetime(str(d.posting_date)+' '+str(d.posting_time))<=self.datetime(end))]
    def test_exact_two_invoice_rows_and_server_totals_not_client_payload(self):
        self.populate(self.doc)
        self.assertEqual(self.doc.pos_transactions, [
            Doc(pos_invoice=name, posting_date='2026-10-20', grand_total=25, customer='Walk In')
            for name in ('STG00002','STG00003')])
        self.assertEqual((self.doc.grand_total,self.doc.net_total,self.doc.total_quantity),(50,40,2))
        self.assertEqual(self.doc.taxes,[Doc(account_head='VAT',rate=25,amount=10)])
        pay = self.doc.payment_reconciliation[0]
        self.assertEqual((pay.opening_amount,pay.expected_amount,pay.closing_amount,pay.difference),(0,50,50,0))
        self.assertEqual(self.doc.period_start_date,self.opening.period_start_date)
        self.assertEqual(self.doc.period_end_date,self.now)
        self.assertEqual(self.query_count,1)
        self.assertEqual('POS Opening Entry',self.locks[0][0])
        self.assertEqual([args[0] for query,args in self.locks[1:]],['STG00002','STG00003'])
    def test_injected_foreign_refs_and_totals_are_replaced(self):
        self.doc.pos_transactions=[Doc(pos_invoice='FOREIGN-INVOICE',grand_total=9999)]
        self.populate(self.doc)
        self.assertEqual([d.pos_invoice for d in self.doc.pos_transactions],['STG00002','STG00003'])
        self.assertEqual(self.doc.grand_total,50)
    def test_submission_rebuilds_and_preserves_counted_amount(self):
        self.populate(self.doc)
        self.doc.docstatus=1
        self.doc.previous=Doc(docstatus=0)
        self.doc.payment_reconciliation[0].closing_amount=48
        self.doc.grand_total=1
        self.doc.pos_transactions=[]
        self.populate(self.doc)
        self.assertEqual(self.doc.grand_total,50)
        self.assertEqual(len(self.doc.pos_transactions),2)
        self.assertEqual(self.doc.payment_reconciliation[0].difference,-2)
    def test_opening_session_profile_company_status_guards(self):
        for target, field, value in [(self.opening,'user','other'),(self.doc,'user','other'),
            (self.doc,'pos_profile','other'),(self.doc,'company','other'),
            (self.profile,'company','other'),(self.opening,'status','Closed'),
            (self.opening,'docstatus',0),(self.opening,'period_start_date','2100-01-01')]:
            with self.subTest(field=field,value=value):
                before=target[field];target[field]=value
                with self.assertRaises(Refused):self.populate(self.doc)
                target[field]=before
        self.assertEqual(self.query_count,0)
    def test_native_read_permission_still_required(self):
        for target in (self.opening,self.profile,self.invoices['STG00002']):
            target.denied=True
            with self.assertRaises(Refused):self.populate(self.doc)
            target.pop('denied')
    def test_query_excludes_draft_consolidated_foreign_owner_profile_and_period(self):
        for field,value in [('docstatus',0),('consolidated_invoice','SI-1'),('owner','other'),
                            ('pos_profile','other'),('posting_date','2025-01-01')]:
            extra=copy.deepcopy(self.invoices['STG00002']);extra.name='EXCLUDED';extra[field]=value
            self.invoices['EXCLUDED']=extra
            self.populate(self.doc)
            self.assertEqual([r.pos_invoice for r in self.doc.pos_transactions],['STG00002','STG00003'])
            del self.invoices['EXCLUDED']
    def test_returned_invoice_wrong_company_cashier_or_branch_refused(self):
        invoice=self.invoices['STG00002']
        for field in ('company','cashier','branch'):
            before=invoice[field];invoice[field]='other'
            with self.assertRaises(Refused):self.populate(self.doc)
            invoice[field]=before
    def test_cancel_between_discovery_and_lock_refuses_stale_closing(self):
        self.after_lock=lambda:self.invoices['STG00002'].update(docstatus=2)
        with self.assertRaisesRegex(Refused,'Ineligible'):self.populate(self.doc)
        self.assertEqual(self.doc.pos_transactions,[])
    def test_locked_read_includes_existing_new_invoice(self):
        extra=copy.deepcopy(self.invoices['STG00002']);extra.name='LATE-INVOICE'
        self.invoices['LATE-INVOICE']=extra
        self.populate(self.doc)
        self.assertEqual(self.doc.grand_total,75)
        self.assertEqual(len(self.doc.pos_transactions),3)
    def test_arithmetic_matches_exact_pinned_native_builder(self):
        reference=self.namespace['make_closing_entry_from_opening'](self.opening)
        self.populate(self.doc)
        for field in ('pos_transactions','taxes','grand_total','net_total','total_quantity'):
            self.assertEqual(self.doc[field],reference[field])
    def test_rebuild_reads_payment_and_tax_changes_after_lock(self):
        self.after_lock=lambda:self.invoices['STG00002'].update(grand_total=30,net_total=25,
            payments=[Doc(mode_of_payment='Clip',amount=30)])
        self.populate(self.doc)
        self.assertEqual(self.doc.grand_total,55)
        self.assertEqual(self.doc.payment_reconciliation[0].expected_amount,55)
    def test_duplicate_modes_missing_modes_and_nonfinite_counts_refused(self):
        original=copy.deepcopy(self.doc.payment_reconciliation)
        for rows in [[],original+original,[Doc(mode_of_payment='Foreign',closing_amount=50)],
                     [Doc(mode_of_payment='Clip',closing_amount=float('nan'))],
                     [Doc(mode_of_payment='Clip',closing_amount='not money')]]:
            self.doc.payment_reconciliation=rows
            with self.assertRaises(Refused):self.populate(self.doc)
    def test_native_submit_sends_exact_two_refs_to_synchronous_merge_boundary(self):
        self.populate(self.doc)
        received=[]
        namespace={'frappe':SimpleNamespace(flags=SimpleNamespace(in_test=False)),
            'get_invoice_customer_map':lambda rows:tuple(row.pos_invoice for row in rows),
            'create_merge_logs':lambda grouped, closing:received.append((grouped,closing)),
            'enqueue_job':lambda *args,**kwargs:self.fail('Two invoices must be synchronous')}
        load_functions(ROOT/'tests/fixtures/erpnext_pos_invoice_merge_log_4cea6a7.py',
                       ['consolidate_pos_invoices'],namespace)
        tree=ast.parse(NATIVE.read_text())
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='POSClosingEntry')
        submit=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='on_submit')
        namespace['frappe']=SimpleNamespace(flags=SimpleNamespace(in_test=False),
                                           publish_realtime=lambda *args,**kwargs:None)
        exec(compile(ast.Module(body=[submit],type_ignores=[]),str(NATIVE),'exec'),namespace)
        namespace['on_submit'](self.doc)
        self.assertEqual(received,[(('STG00002','STG00003'),self.doc)])
    def test_native_queued_consolidation_leaves_open_shift_but_blocks_second_close(self):
        for index in range(8):
            invoice=copy.deepcopy(self.invoices['STG00002'])
            invoice.name=f'QUEUE-{index}'
            self.invoices[invoice.name]=invoice
        self.populate(self.doc)
        queued=[]
        namespace={'frappe':SimpleNamespace(flags=SimpleNamespace(in_test=False)),
            'get_invoice_customer_map':lambda rows:tuple(row.pos_invoice for row in rows),
            'create_merge_logs':lambda *args:self.fail('Ten invoices should queue'),
            'enqueue_job':lambda *args,**kwargs:queued.append(kwargs)}
        load_functions(ROOT/'tests/fixtures/erpnext_pos_invoice_merge_log_4cea6a7.py',
                       ['consolidate_pos_invoices'],namespace)
        self.doc.docstatus=1
        namespace['consolidate_pos_invoices'](closing_entry=self.doc)
        self.assertEqual(len(queued),1)
        self.assertEqual(self.doc.status,'Queued')
        self.assertEqual(self.opening.status,'Open')
        self.existing_closings.append(copy.deepcopy(self.doc))
        other=copy.deepcopy(self.doc);other.name='CLOSE-2';other.docstatus=0
        with self.assertRaisesRegex(Refused,'existing closing'):self.populate(other)
    def test_other_draft_and_failed_closings_block_but_current_and_cancelled_do_not(self):
        for status,state in [(0,'Draft'),(1,'Failed'),(1,'Submitted')]:
            self.existing_closings=[Doc(name='OTHER',pos_opening_entry='OPEN-1',
                                       docstatus=status,status=state)]
            with self.assertRaisesRegex(Refused,'existing closing'):self.populate(self.doc)
        for other in [Doc(name='CLOSE-1',pos_opening_entry='OPEN-1',docstatus=0),
                      Doc(name='OLD-CANCELLED',pos_opening_entry='OPEN-1',docstatus=2)]:
            self.existing_closings=[other]
            self.populate(self.doc)
    def test_second_closing_of_closed_opening_refused(self):
        self.populate(self.doc)
        self.opening.status='Closed'
        with self.assertRaises(Refused):self.populate(self.doc)
    def test_empty_shift_is_genuine_zero_not_client_fabricated_sales(self):
        self.invoices.clear()
        self.populate(self.doc)
        self.assertEqual((self.doc.pos_transactions,self.doc.grand_total,self.doc.total_quantity),([],0,0))
    def test_historical_submitted_and_cancellation_not_rewritten(self):
        for status,previous in [(1,Doc(docstatus=1)),(2,Doc(docstatus=1))]:
            self.doc.docstatus=status;self.doc.previous=previous
            self.populate(self.doc)
        self.assertEqual(self.query_count,0)
    def test_pinned_primary_source_fixture_hashes(self):
        for name,digest in {
            'erpnext_pos_closing_entry_4cea6a7.py':'b778497c456b8beffea07623b203e8519933ac8d3fbcc954f443954821814252',
            'erpnext_pos_invoice_merge_log_4cea6a7.py':'415b703c503f6e4e2072f6a76c2c6d068573610502af028731182212f1316b02'
        }.items():
            self.assertEqual(hashlib.sha256((ROOT/'tests/fixtures'/name).read_bytes()).hexdigest(),digest)
    def test_hook_registered_before_native_validation(self):
        hooks=ast.parse((ROOT/'ury/hooks.py').read_text())
        mapping=next(n.value for n in hooks.body if isinstance(n,ast.Assign)
                     and any(isinstance(t,ast.Name) and t.id=='doc_events' for t in n.targets))
        events=ast.literal_eval(mapping)['POS Closing Entry']
        self.assertEqual(events['before_validate'],'ury.ury.hooks.ury_pos_closing_entry.populate_server_closing')
        self.assertEqual(events['validate'],'ury.ury.hooks.ury_pos_closing_entry.validate')
        self.assertEqual(events['before_save'],'ury.ury.hooks.ury_pos_closing_entry.before_save')

if __name__=='__main__':unittest.main()
