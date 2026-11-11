"""Verify release proof binds only the committed platform subtree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
from git_release_proof import make_proof, verify_proof

class GitProofTest(unittest.TestCase):
    def test_commit_tree_and_file_changes_are_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo=Path(tmp)/'repo';repo.mkdir()
            def git(*args): return subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True).stdout
            git('init','-q');git('config','user.name','Fixture');git('config','user.email','fixture@example.invalid')
            platform=repo/'pro6000d-live-platform'; (platform/'a').mkdir(parents=True); (platform/'b').mkdir()
            (platform/'a'/'file').write_text('same\n'); (platform/'b'/'file').write_text('same\n')
            research=repo/'research'/'pro6000d'; research.mkdir(parents=True); (research/'candidate.patch').write_text('patch\n')
            (repo/'unrelated-secret').write_text('not exported\n')
            git('add','pro6000d-live-platform','research','unrelated-secret');git('commit','-qm','fixture')
            commit=git('rev-parse','HEAD').decode().strip(); proof=Path(tmp)/'proof.json'
            proof.write_text(json.dumps(make_proof(repo,commit)))
            self.assertNotIn('not exported',proof.read_text())
            verify_proof(platform,proof,commit,owner=os.geteuid(),check_ancestors=False)
            (platform/'a'/'file').write_text('changed\n')
            with self.assertRaises(ValueError): verify_proof(platform,proof,commit,owner=os.geteuid(),check_ancestors=False)
            (platform/'a'/'file').write_text('same\n'); (platform/'extra').write_text('extra')
            with self.assertRaises(ValueError): verify_proof(platform,proof,commit,owner=os.geteuid(),check_ancestors=False)

if __name__=='__main__': unittest.main()
