"""Verify reviewed originals and register a private, immutable source bundle.

No roster writes, network requests or import actions. Facts must be reviewed
against the official PDF; registration reparses and checks every supplied fact.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from core import SafetyStop, read_roster, file_hash
from metadata_identity import title_key
from primary_metadata import rebuild_primary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--review', required=True, type=Path)
    args = parser.parse_args()
    root = args.review.resolve()
    manifest = json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    facts = json.loads((root/'reviewed-facts.json').read_text(encoding='utf-8'))
    roster = read_roster(BASE/'list.xlsx')
    registry = BASE/'runtime/metadata-imports/primary-evidence'
    registry.mkdir(parents=True, exist_ok=True)
    for entry in manifest:
        slug = entry['slug']; folder = root/'sources'/slug
        if slug not in facts or folder.is_symlink() or not folder.resolve().is_relative_to(root):
            raise SafetyStop('未确认的来源资料目录。')
        documents = [{'path':name, 'url':entry[key], 'sha256':file_hash(folder/name)}
                     for name,key in (('source.html','page'),('source.pdf','pdf'))]
        evidence = {'version':1, 'reviewed':True, 'documents':documents, 'facts':facts[slug]}
        records = [r for r in roster.records if r.owner == '谭勋策' and r.matches == 0
                   and title_key(r.title) == title_key(entry['title'])]
        if not records:
            raise SafetyStop('来源没有对应的负责人零匹配名单。')
        candidates = [rebuild_primary(record, folder, evidence) for record in records]
        key = hashlib.sha256(title_key(entry['title']).encode('utf-8')).hexdigest()
        target = registry/key
        if target.is_symlink():
            raise SafetyStop('来源登记目录是链接。')
        target.mkdir(exist_ok=True)
        for name in ('source.html','source.pdf'):
            dest = target/name
            if dest.is_symlink() or dest.exists() and file_hash(dest) != file_hash(folder/name):
                raise SafetyStop('已登记的原始资料不同，未覆盖。')
            if not dest.exists():
                with dest.open('xb') as out, (folder/name).open('rb') as original:
                    shutil.copyfileobj(original, out)
        dest = target/'evidence.json'
        if dest.exists():
            if dest.is_symlink() or json.loads(dest.read_text(encoding='utf-8')) != evidence:
                raise SafetyStop('已登记的人工核验资料不同，未覆盖。')
        else:
            with dest.open('x',encoding='utf-8') as stream:
                json.dump(evidence,stream,ensure_ascii=False,indent=2)
        print(json.dumps({'source':slug, 'verified_records':len(records),
                          'authors':len(candidates[0]['author_names']), 'doi':candidates[0]['doi'],
                          'pending_rows':[r.row for r in records if not r.done and not r.skipped]},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
