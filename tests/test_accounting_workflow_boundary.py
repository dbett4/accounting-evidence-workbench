"""CSV grammar, malformed retained data and actual downstream refusal checks."""
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from acfr_factory.accounting_evidence import evaluate_manifest, pack_for, verify_pack
from acfr_factory.link_contract import fingerprint


class WorkflowBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.inputs = self.root / 'inputs'
        shutil.copytree(ROOT / 'examples/accounting-evidence', self.inputs)
        self.manifest = self.inputs / 'manifest.json'; self.ledger = self.inputs / 'ledger.csv'

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/accounting_evidence.py'), *map(str, args)],capture_output=True,text=True)

    def pin_ledger(self):
        doc = json.loads(self.manifest.read_text())
        doc['ledger']['sha256'] = hashlib.sha256(self.ledger.read_bytes()).hexdigest()
        self.manifest.write_text(json.dumps(doc))

    def corrupt_quote(self):
        lines = self.ledger.read_text().splitlines()
        self.ledger.write_text(lines[0] + ',Description\n' + '\n'.join(
            line + (',"unterminated' if n == len(lines)-1 else ',retained annotation')
            for n, line in enumerate(lines[1:],1)) + '\n')
        self.pin_ledger()

    def test_unterminated_annotation_blocks_all_claims_and_retains_open_review(self):
        self.corrupt_quote(); source = self.ledger.read_bytes()
        r = self.cli('explain', self.manifest, '--out', self.root/'review')
        self.assertEqual(r.returncode,1,r.stdout); summary=json.loads(r.stdout)
        self.assertEqual(summary['supported_claims'],[])
        pack=json.loads((Path(summary['output'])/'pack.json').read_text())
        for claim in pack['report']['claims']:
            self.assertEqual(claim['status'],'unresolved')
            self.assertIn('invalid_ledger_csv',{p['code'] for p in claim['issues']})
            self.assertIn('physical line 7',next(p['detail'] for p in claim['issues'] if p['code']=='invalid_ledger_csv'))
        self.assertTrue(all(e['status']=='open' for e in pack['report']['exception_register']['exceptions']))
        self.assertEqual(pack['snapshot']['files'][0]['content'].encode(),source)
        self.assertTrue((Path(summary['output'])/'review.md').is_file())
        self.assertTrue(Path(summary['review_html']).is_file())
        self.assertEqual(verify_pack(pack,summary['pack_sha256'])['status'],'fail')
        self.assertEqual(self.ledger.read_bytes(),source)

    def test_valid_quoted_multiline_annotation_remains_supported_and_retained(self):
        reader=csv.DictReader(io.StringIO(self.ledger.read_text()));fields=reader.fieldnames+['Description'];rows=list(reader)
        out=io.StringIO(newline='');writer=csv.DictWriter(out,fields,lineterminator='\n');writer.writeheader()
        writer.writerows({**row,'Description':'Retained "annotation"\nsecond physical line'} for row in rows)
        self.ledger.write_text(out.getvalue());self.pin_ledger();pack=pack_for(self.manifest)
        self.assertEqual(pack['report']['status'],'pass')
        self.assertEqual(pack['report']['claims'][0]['ending_cents'],1_090_000)
        self.assertEqual(pack['snapshot']['files'][0]['content'],out.getvalue())

    def test_truncated_optional_annotation_column_is_a_row_error(self):
        self.ledger.write_text(self.ledger.read_text().replace('evidence_id\n','evidence_id,Description\n'));self.pin_ledger()
        report=evaluate_manifest(self.manifest)
        self.assertEqual(report['status'],'fail')
        self.assertTrue(all('invalid_ledger_row' in {p['code'] for p in c['issues']} for c in report['claims']))

    def test_unterminated_header_emits_an_unresolved_pack_instead_of_a_traceback(self):
        self.ledger.write_text('"'+self.ledger.read_text());self.pin_ledger()
        r=self.cli('explain',self.manifest,'--out',self.root/'review')
        self.assertEqual(r.returncode,1,r.stdout);self.assertEqual(r.stderr,'')
        pack=json.loads((Path(json.loads(r.stdout)['output'])/'pack.json').read_text())
        self.assertIn('invalid_ledger_csv',{p['code'] for p in pack['report']['claims'][0]['issues']})

    def test_malformed_snapshot_shapes_return_structured_blocking_json(self):
        original=pack_for(self.manifest)['snapshot']
        snapshots=[None,[],True,{**original,'manifest':None},{**original,'files':{}},{**original,'files':[None]},{**original,'files':['not a source record']}]
        for n,snapshot in enumerate(snapshots):
            with self.subTest(snapshot=n):
                pack={'schema_version':'accounting-evidence/1','snapshot':snapshot,'report':{}}
                p=self.root/f'bad-{n}.json';p.write_text(json.dumps(pack))
                r=self.cli('verify',p,'--expected-sha256',fingerprint(pack))
                self.assertEqual(r.returncode,2);self.assertEqual(r.stderr,'')
                self.assertEqual(json.loads(r.stdout)['status'],'blocked')

    def test_invalid_snapshot_refuses_all_dependent_commands_before_writing(self):
        pack={'schema_version':'accounting-evidence/1','snapshot':None,'report':{}}
        p=self.root/'invalid.json';p.write_text(json.dumps(pack));digest=fingerprint(pack)
        good=pack_for(self.manifest);q=self.root/'good.json';q.write_text(json.dumps(good))
        commands=[['view',p,'--expected-sha256',digest,'--output',self.root/'view.html'],
                  ['review-template',p,'--expected-sha256',digest,'--output',self.root/'notes.json'],
                  ['review',p,self.root/'absent-notes.json','--expected-sha256',digest,'--out',self.root/'review'],
                  ['compare',q,p,'--before-sha256',fingerprint(good),'--after-sha256',digest]]
        for command in commands:
            r=self.cli(*command);self.assertEqual(r.returncode,2);self.assertEqual(r.stderr,'')
            self.assertEqual(json.loads(r.stdout)['status'],'blocked')
        self.assertFalse((self.root/'view.html').exists());self.assertFalse((self.root/'notes.json').exists());self.assertFalse((self.root/'review').exists())

    def test_native_library_rejects_nonobject_packs_and_nontext_digests(self):
        for pack in (None,[],True,'not a pack'):
            with self.assertRaises(ValueError):verify_pack(pack,fingerprint(pack))
        pack=pack_for(self.manifest)
        for digest in (None,42,{},True):
            with self.assertRaises(ValueError):verify_pack(pack,digest)


    def test_invalid_csv_basis_cannot_generate_bank_link_or_invoice_templates(self):
        self.corrupt_quote()
        bank=self.inputs/'manifest.bank.json';doc=json.loads(bank.read_text())
        doc['ledger']=json.loads(self.manifest.read_text())['ledger'];bank.write_text(json.dumps(doc))
        r=self.cli('bank-links-template',bank,'--claim','cash','--output',self.root/'bank-template.json')
        self.assertEqual(r.returncode,2);self.assertFalse((self.root/'bank-template.json').exists())
        ar=self.inputs/'receivables/manifest.json';doc=json.loads(ar.read_text())
        doc['ledger']={'path':'../ledger.csv','sha256':hashlib.sha256(self.ledger.read_bytes()).hexdigest(),'completeness':'provided_extract_only'}
        ar.write_text(json.dumps(doc))
        r=self.cli('receivables-template',ar,'--claim','receivables','--output',self.root/'ar-template.json')
        self.assertEqual(r.returncode,2);self.assertFalse((self.root/'ar-template.json').exists())

    def test_changed_html_line_endings_cannot_receive_the_wrong_byte_digest(self):
        r=self.cli('explain',self.manifest,'--out',self.root/'review');self.assertEqual(r.returncode,0)
        summary=json.loads(r.stdout);p=Path(summary['review_html']);changed=p.read_bytes().replace(b'\n',b'\r\n');p.write_bytes(changed)
        r=self.cli('explain',self.manifest,'--out',self.root/'review')
        self.assertEqual(r.returncode,2);self.assertEqual(json.loads(r.stdout)['status'],'blocked')
        self.assertEqual(p.read_bytes(),changed)
        self.assertNotEqual(hashlib.sha256(changed).hexdigest(),summary['html_sha256'])

    def test_recheck_works_offline_preserves_exact_original_bytes_and_is_idempotent(self):
        pack=pack_for(self.manifest);digest=fingerprint(pack)
        raw=b'\xef\xbb\xbf'+(json.dumps(pack,indent=2)+'\n').replace('\n','\r\n').encode()
        p=self.root/'original.json';p.write_bytes(raw);shutil.rmtree(self.inputs)
        args=('recheck',p,'--expected-sha256',digest,'--out',self.root/'rechecked')
        first=self.cli(*args);second=self.cli(*args)
        self.assertEqual((first.returncode,second.returncode),(0,0),first.stdout)
        self.assertEqual(first.stdout,second.stdout)
        result=json.loads(first.stdout);self.assertFalse(result['report_changed']);self.assertEqual(result['pack_sha256'],digest)
        self.assertEqual(Path(result['original_pack_copy']).read_bytes(),raw);self.assertEqual(p.read_bytes(),raw)
        receipt=json.loads(Path(result['recheck_receipt']).read_text())
        self.assertEqual(receipt['original_file_sha256'],hashlib.sha256(raw).hexdigest())
        self.assertEqual(receipt['evaluator_sha256'],fingerprint(receipt['evaluator']))
        self.assertEqual(receipt['retained_snapshot_sha256'],fingerprint(pack['snapshot']))

    def test_recheck_ignores_saved_amounts_and_retains_the_prior_report(self):
        original=pack_for(self.manifest);original['report']['claims'][0]['ending_cents']+=1
        digest=fingerprint(original);p=self.root/'prior.json';p.write_text(json.dumps(original));before=p.read_bytes()
        self.assertEqual(self.cli('verify',p,'--expected-sha256',digest).returncode,2)
        r=self.cli('recheck',p,'--expected-sha256',digest,'--out',self.root/'rechecked')
        self.assertEqual(r.returncode,0,r.stdout);result=json.loads(r.stdout)
        self.assertTrue(result['report_changed']);self.assertNotEqual(result['pack_sha256'],digest)
        current=json.loads((Path(result['output'])/'pack.json').read_text())
        self.assertEqual(current['report']['claims'][0]['ending_cents'],1_090_000)
        self.assertEqual(current['snapshot'],original['snapshot']);self.assertEqual(p.read_bytes(),before)
        self.assertEqual(Path(result['original_pack_copy']).read_bytes(),before)

    def test_recheck_retains_false_saved_success_and_emits_current_open_failures(self):
        self.corrupt_quote();original=pack_for(self.manifest)
        original['report']['status']='pass'
        for claim in original['report']['claims']:claim['status']='supported'
        digest=fingerprint(original);p=self.root/'prior.json';p.write_text(json.dumps(original));before=p.read_bytes();shutil.rmtree(self.inputs)
        r=self.cli('recheck',p,'--expected-sha256',digest,'--out',self.root/'rechecked')
        self.assertEqual(r.returncode,1,r.stdout);result=json.loads(r.stdout)
        self.assertTrue(result['report_changed']);current=json.loads((Path(result['output'])/'pack.json').read_text())
        self.assertEqual(verify_pack(current,result['pack_sha256'])['status'],'fail')
        self.assertTrue(all(c['status']=='unresolved' for c in current['report']['claims']))
        self.assertIn('invalid_ledger_csv',{p['code'] for p in current['report']['claims'][0]['issues']})
        self.assertTrue(all(e['status']=='open' for e in current['report']['exception_register']['exceptions']))
        self.assertEqual(p.read_bytes(),before);self.assertEqual(Path(result['original_pack_copy']).read_bytes(),before)

    def test_recheck_refuses_bad_digest_or_snapshot_without_writing(self):
        original=pack_for(self.manifest);p=self.root/'pack.json';p.write_text(json.dumps(original))
        for digest in ('0'*64,'bad'):
            r=self.cli('recheck',p,'--expected-sha256',digest,'--out',self.root/'out')
            self.assertEqual(r.returncode,2);self.assertEqual(json.loads(r.stdout)['status'],'blocked')
        original['snapshot']=None;p.write_text(json.dumps(original))
        r=self.cli('recheck',p,'--expected-sha256',fingerprint(original),'--out',self.root/'out')
        self.assertEqual(r.returncode,2);self.assertEqual(r.stderr,'');self.assertFalse((self.root/'out').exists())


if __name__=='__main__':unittest.main()
