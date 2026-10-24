// A single flat, typed sheet matching the native general-Excel field mapper.
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
const [input, output, moduleRoot] = process.argv.slice(2);
if (!input || !output || !path.isAbsolute(moduleRoot || '')) throw Error('Missing Excel runtime/input');
const require = createRequire(path.join(moduleRoot, '__sa_artifact_runtime.cjs'));
const {Workbook, SpreadsheetFile} = await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const payload = JSON.parse(await fs.readFile(input, 'utf8'));
const c = payload.candidate;
const primary = c?.kind === 'publisher-verified-v1' && /^[a-f0-9]{64}$/.test(c.primary_evidence_sha256??'') &&
  c.fields?.[c.document_type==='journal-article'?'URL':'相关网址'] === c.primary_url &&
  ['ICLR官方论文集','PMLR官方论文集','Nature出版社','SciOpen出版社'].includes(c.source);
if ((!primary && (c?.kind !== 'crossref-openalex-v1' || !c.doi)) || !c.sjtu || !c.fields) throw Error('Unverified metadata');
const headers = Object.keys(c.fields), values = headers.map(k => c.fields[k]);
if (headers.length < 7 || values.some(v => typeof v !== 'string')) throw Error('Invalid flat data');
const wb = Workbook.create(), sheet = wb.worksheets.add('论文数据');
const last = n => {let s='';for(;n;n=Math.floor((n-1)/26))s=String.fromCharCode(65+(n-1)%26)+s;return s;};
const range = `A1:${last(headers.length)}2`;
sheet.getRange(range).setNumberFormat('@');
sheet.getRange(range).values = [headers, values];
sheet.getRange(range).format.font = {name:'Arial',size:10};
sheet.getRange(`A1:${last(headers.length)}1`).format.fill = '#E7EEF5';
sheet.getRange(`A1:${last(headers.length)}1`).format.font.bold = true;
sheet.getRange(range).format.columnWidth = 25;
sheet.getRange('A1:D2').format.columnWidth = 45;
sheet.freezePanes.freezeRows(1);
wb.recalculate();
const inspection = await wb.inspect({kind:'table',range:`论文数据!${range}`,include:'values,formulas',tableMaxRows:2,tableMaxCols:headers.length});
await fs.writeFile(output+'.inspection.json',JSON.stringify(inspection));
const preview = await wb.render({sheetName:'论文数据',range:'A1:D2',scale:1.2});
await fs.writeFile(output+'.preview.png',new Uint8Array(await preview.arrayBuffer()));
await (await SpreadsheetFile.exportXlsx(wb)).save(output);
process.stdout.write('Verified single-paper Excel exported\n');
