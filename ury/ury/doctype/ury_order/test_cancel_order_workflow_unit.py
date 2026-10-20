"""Actual cancel_order function with native API boundary fakes; no DB or bench.

The draft audit/link-check behavior modeled here is a unit contract, not proof
of a real Frappe Deleted Document. Integration must also exercise native hooks.
"""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).with_name('ury_order.py')

class Denied(Exception):
    pass

class NativeInvoice:
    def __init__(self, status):
        self.name = 'UNIT-INVOICE'
        self.docstatus = status
        self.branch = 'UNIT-BRANCH'
        self.restaurant_table = 'UNIT-TABLE'
        self.cancel_reason = None
        self.items = [{'item_code': 'UNIT-ITEM', 'qty': 1}]
        self.calls = []
        self.native_failure = False

    def db_set(self, field, value):
        assert field == 'cancel_reason'
        self.calls.append(('reason', value))
        self.cancel_reason = value

    def cancel(self):
        self.calls.append(('cancel',))
        if self.docstatus != 1:
            raise Denied('Native Frappe rejects Draft to Cancelled')
        if self.native_failure:
            raise Denied('Native accounting hook refused cancellation')
        self.docstatus = 2

class CancellationTests(unittest.TestCase):
    def setUp(self):
        self.invoice = NativeInvoice(0)
        self.can_cancel = True
        self.branch = 'UNIT-BRANCH'
        self.events = []
        self.deleted = []
        self.linked = False
        self.kot_failure = False
        self.frappe = SimpleNamespace(whitelist=lambda: lambda function: function,
            session=SimpleNamespace(user='unit-staff@example.invalid'), PermissionError=Denied,
            get_doc=self.get_doc, has_permission=self.has_permission,
            throw=lambda message, *args: self.refuse(message), delete_doc=self.delete_doc)
        node = next(n for n in ast.parse(SOURCE.read_bytes()).body
                    if isinstance(n, ast.FunctionDef) and n.name == 'cancel_order')
        namespace = {'frappe': self.frappe, '_': lambda value: value,
                     'getBranch': lambda: self.branch, 'cancel_kot': self.cancel_kot,
                     'release_merge_cluster_tables': self.release}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), 'exec'), namespace)
        self.cancel = namespace['cancel_order']

    def refuse(self, message):
        raise Denied(message)

    def get_doc(self, doctype, name):
        self.assertEqual((doctype, name), ('POS Invoice', self.invoice.name))
        return self.invoice

    def has_permission(self, doctype, permission, *, doc):
        self.assertEqual((doctype, permission, doc), ('POS Invoice', 'cancel', self.invoice))
        self.events.append('permission')
        return self.can_cancel

    def cancel_kot(self, name):
        self.assertEqual(name, self.invoice.name)
        self.events.append('kot')
        if self.kot_failure:
            raise RuntimeError('Optional KOT integration missing')

    def delete_doc(self, doctype, name, **kwargs):
        self.assertEqual((doctype, name), ('POS Invoice', self.invoice.name))
        self.assertEqual(kwargs, {'ignore_permissions': True, 'delete_permanently': False})
        self.assertTrue(self.can_cancel)
        self.assertEqual(self.branch, self.invoice.branch)
        self.assertEqual(self.invoice.docstatus, 0)
        if self.linked:
            raise Denied('Native live-link check refused deletion')
        self.deleted.append(copy.deepcopy(vars(self.invoice)))
        self.events.append('delete')

    def release(self, table):
        self.assertEqual(table, self.invoice.restaurant_table)
        self.assertTrue(self.deleted or self.invoice.docstatus == 2)
        self.events.append('release-and-commit')

    def test_draft_uses_recoverable_native_delete_under_cancel_authority(self):
        # This cashier intentionally has no independent generic delete right.
        self.cancel(self.invoice.name, '  Customer changed their mind  ')
        self.assertEqual(self.invoice.calls, [('reason', 'Customer changed their mind')])
        self.assertEqual(self.deleted[0]['cancel_reason'], 'Customer changed their mind')
        self.assertEqual(self.deleted[0]['items'], self.invoice.items)
        self.assertEqual(self.events[-2:], ['delete', 'release-and-commit'])

    def test_submitted_calls_native_cancel_once_without_delete(self):
        self.invoice.docstatus = 1
        self.cancel(self.invoice.name, 'Unit reason')
        self.assertEqual(self.invoice.calls, [('reason', 'Unit reason'), ('cancel',)])
        self.assertEqual(self.invoice.docstatus, 2)
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.events[-1], 'release-and-commit')

    def test_cancel_permission_denial_precedes_all_mutations(self):
        self.can_cancel = False
        with self.assertRaises(Denied):
            self.cancel(self.invoice.name, 'Unit reason')
        self.assertEqual(self.invoice.calls, [])
        self.assertEqual(self.events, ['permission'])

    def test_cross_branch_denied_before_all_mutations(self):
        self.branch = 'FOREIGN-BRANCH'
        with self.assertRaises(Denied):
            self.cancel(self.invoice.name, 'Unit reason')
        self.assertEqual(self.invoice.calls, [])
        self.assertEqual(self.events, ['permission'])

    def test_cancelled_invalid_or_missing_reason_refused_before_mutation(self):
        for state, reason in ((2, 'Unit'), (3, 'Unit'), (0, ''), (0, '  '), (1, None)):
            with self.subTest(state=state, reason=reason):
                self.setUp()
                self.invoice.docstatus = state
                with self.assertRaises(Denied):
                    self.cancel(self.invoice.name, reason)
                self.assertEqual(self.invoice.calls, [])
                self.assertEqual(self.events, ['permission'])

    def test_link_refusal_does_not_release_or_commit_tables(self):
        self.linked = True
        with self.assertRaises(Denied):
            self.cancel(self.invoice.name, 'Unit reason')
        self.assertNotIn('release-and-commit', self.events)
        self.assertEqual(self.deleted, [])

    def test_submitted_hook_refusal_does_not_release_tables(self):
        self.invoice.docstatus = 1
        self.invoice.native_failure = True
        with self.assertRaises(Denied):
            self.cancel(self.invoice.name, 'Unit reason')
        self.assertNotIn('release-and-commit', self.events)
        self.assertEqual(self.deleted, [])

    def test_optional_kot_failure_preserves_native_lifecycle(self):
        self.kot_failure = True
        self.cancel(self.invoice.name, 'Unit reason')
        self.assertEqual(len(self.deleted), 1)
        self.assertEqual(self.events[-1], 'release-and-commit')

if __name__ == '__main__':
    unittest.main()
