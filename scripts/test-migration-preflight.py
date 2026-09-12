"""Preflight must reject paths the upgrade copy set would omit, without writes."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[1]
SCRIPT=REPO/'installer/migrate-state.py'
spec=importlib.util.spec_from_file_location('migration_preflight',SCRIPT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


class PreflightTests(unittest.TestCase):
    def test_preserved_default_and_nested_state_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for relative in ('.state/progress-wx.sqlite','.state/nested/business.sqlite'):
                database=root/relative
                self.assertEqual(module.validate_preserved_paths(root,database),database.resolve())
            self.assertFalse((root/'.state').exists())

    def test_root_internal_and_external_unpreserved_paths_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'install';root.mkdir()
            for database in (root/'data/business.sqlite',root/'business.sqlite',root/'.state-old/db.sqlite',root.parent/'outside.sqlite'):
                with self.subTest(database=database.name),self.assertRaisesRegex(RuntimeError,'preserved .state'):
                    module.validate_preserved_paths(root,database)

    def test_guardian_redirect_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.state').mkdir();(root/'other').mkdir()
            try:(root/'.state/guardian').symlink_to(root/'other',target_is_directory=True)
            except OSError:self.skipTest('Directory symlink permission unavailable')
            with self.assertRaisesRegex(RuntimeError,'reparse link'):
                module.validate_preserved_paths(root,root/'.state/db.sqlite')

    @unittest.skipUnless(os.name == 'nt', 'Windows junction test')
    def test_real_cli_rejects_resolved_internal_junction(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            target=root/'.state/real';target.mkdir(parents=True)
            database=target/'db.sqlite';database.write_bytes(b'synthetic-unchanged')
            link=root/'.state/link'
            # One PowerShell process creates and removes only the exact junction.
            environment=dict(os.environ, PREFLIGHT_LINK=str(link), PREFLIGHT_TARGET=str(target),
                             PYTHONPATH=str(REPO/'components/codex-feishu/src'))
            created=subprocess.run(['powershell.exe','-NoProfile','-Command',
                "New-Item -ItemType Junction -Path $env:PREFLIGHT_LINK -Target $env:PREFLIGHT_TARGET -ErrorAction Stop | Out-Null"],
                env=environment,capture_output=True,timeout=15)
            self.assertEqual(created.returncode,0,created.stderr.decode(errors='replace'))
            try:
                self.assertEqual((link/'db.sqlite').resolve(),database.resolve())
                template=(REPO/'components/codex-feishu/config.example.yaml').read_text(encoding='utf-8-sig')
                (root/'config.yaml').write_text(template.replace('database: ".state/progress-wx.sqlite"',
                    'database: ".state/link/db.sqlite"'),encoding='utf-8')
                result=subprocess.run([sys.executable,'-B',str(SCRIPT),str(root),'--check-only'],
                    env=environment,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,1,result.stdout.decode(errors='replace'))
                self.assertIn(b'reparse link',result.stderr)
                self.assertEqual(database.read_bytes(),b'synthetic-unchanged')
                self.assertFalse((target/'guardian').exists())
            finally:
                self.assertEqual(link.parent.resolve(),root/'.state')
                self.assertTrue(link.is_junction())
                link.rmdir()  # Remove the junction itself, never recurse into its target.
                self.assertTrue(database.exists())

    def test_real_cli_checks_without_touching_database_or_guardian(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            template=(REPO/'components/codex-feishu/config.example.yaml').read_text(encoding='utf-8-sig')
            environment=dict(os.environ,PYTHONPATH=str(REPO/'components/codex-feishu/src'))
            for location,code in (('.state/progress-wx.sqlite',0),('data/business.sqlite',1)):
                database=root/location;database.parent.mkdir(exist_ok=True)
                database.write_bytes(b'synthetic-not-a-sqlite-file-do-not-open')
                configuration=template.replace('database: ".state/progress-wx.sqlite"',f'database: "{location}"')
                (root/'config.yaml').write_text(configuration,encoding='utf-8')
                before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
                result=subprocess.run([sys.executable,'-B',str(SCRIPT),str(root),'--check-only'],env=environment,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,code,result.stderr.decode(errors='replace'))
                after={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
                self.assertEqual(before,after)
                self.assertFalse((database.parent/'guardian').exists())

    def test_upgrade_preflight_precedes_suspension_and_stop(self):
        text=(REPO/'installer/upgrade.ps1').read_text(encoding='utf-8-sig')
        check=text.index("'migrate-state.py') $LegacyProgress --check-only")
        self.assertLess(check,text.index('Disable-GuardianTaskForUpgrade -InstallRoot'))
        self.assertLess(check,text.index('Enter-UpgradeGuardianMaintenance -InstallRoot'))
        self.assertLess(check,text.index("'config.yaml') stop --timeout"))
        self.assertIn("if ($LASTEXITCODE -ne 0) { throw",text[check:check+350])


if __name__=='__main__':unittest.main(verbosity=2)
