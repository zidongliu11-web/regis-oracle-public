"""Parse faithfully retained industry source tables; no market data or outcome access."""
import re
from bisect import bisect_right
import xml.etree.ElementTree as ET
STOCK=re.compile(r'(?<!\d)(\d{6})(?!\d)')
MAJOR=re.compile(r'(?<!\d)(\d{2})(?!\d)')
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
def following_session(calendar,published):
 if calendar!=sorted(set(calendar)):raise ValueError('source calendar not unique sorted')
 day=int(published.replace('-',''));i=bisect_right(calendar,day)
 return calendar[i] if i<len(calendar) else None
def normalized_code(value):
 if isinstance(value,float):
  if not value.is_integer() or value<0 or value>999999:raise ValueError('invalid numeric stock identifier')
  return str(int(value)).zfill(6)
 text=str(value).strip()
 if not re.fullmatch(r'\d{6}',text):raise ValueError('non-six-digit stock identifier')
 return text
def row_add(rows,code,major,locator):
 if not major or not re.fullmatch(r'\d{2}',major) or not 1<=int(major)<=90:raise ValueError('unknown source industry major class')
 code=normalized_code(code)
 if code in rows:raise ValueError('duplicate source stock identity')
 rows[code]={'source_stock_code':code,'source_industry_major_numeric':major,'source_locator':locator}
def parse_pdf_texts(texts):
 rows={};major=None;page_counts=[];ambiguous=[];all_tokens=0
 for page,text in enumerate(texts,1):
  before=len(rows);tokens=STOCK.findall(text);all_tokens+=len(tokens)
  for line_no,line in enumerate(text.splitlines(),1):
   stocks=list(STOCK.finditer(line));prefix=line[:stocks[0].start()] if stocks else line
   # A two-digit class appears alongside a name or the first stock on a table row.
   # Standalone page numbers are not source classes.
   ms=MAJOR.findall(prefix)
   if len(ms)>1:ambiguous.append({'page':page,'line':line_no,'kind':'multiple_major_tokens'})
   elif ms and (stocks or re.search('[\u4e00-\u9fff]',prefix)):
    if '季度' not in prefix and '门类' not in prefix:major=ms[0]
   if len(stocks)>1:raise ValueError('multiple source stocks on one extracted PDF line')
   if stocks:row_add(rows,stocks[0].group(1),major,{'page':page,'line':line_no})
  page_counts.append(len(rows)-before)
 if ambiguous or len(rows)!=all_tokens:raise ValueError('PDF stock token reconciliation failed')
 if not rows:raise ValueError('empty PDF source membership')
 return rows,{'pages':len(texts),'per_page_rows':page_counts,'six_digit_tokens':all_tokens,'duplicate_rows':0}
def parse_cell_tables(tables):
 rows={};major=None;headers=0;source_stock_cells=0
 for table_no,table in enumerate(tables,1):
  stock_col=None;major_col=None
  for row_no,cells in enumerate(table,1):
   texts=[str(x).strip() for x in cells]
   hs=[i for i,t in enumerate(texts) if '上市公司' in t and ('代码' in t)];hc=[i for i,t in enumerate(texts) if '行业大类代码' in t]
   if hs or hc:
    if len(hs)!=1 or len(hc)!=1:raise ValueError('table source headers ambiguous')
    stock_col,major_col=hs[0],hc[0];headers+=1;continue
   if stock_col is None:continue
   if max(stock_col,major_col)>=len(cells):raise ValueError('table row column shift')
   group=texts[major_col]
   if group:
    if re.fullmatch(r'\d{1,2}(?:\.0)?',group):major=str(int(float(group))).zfill(2)
    else:raise ValueError('non-numeric source industry group')
   raw=cells[stock_col]
   if str(raw).strip():
    source_stock_cells+=1;row_add(rows,raw,major,{'table_or_sheet':table_no,'row':row_no})
 if not headers or not rows or source_stock_cells!=len(rows):raise ValueError('table membership reconciliation failed')
 return rows,{'tables_or_sheets':len(tables),'header_rows':headers,'stock_cells':source_stock_cells,'duplicate_rows':0}
def flat_word_tables(xml):
 root=ET.fromstring(xml);tables=[]
 for table in root.findall('.//w:tbl',NS):
  out=[]
  for row in table.findall('w:tr',NS):
   cells=[]
   for cell in row.findall('w:tc',NS):
    text=''.join(t.text or '' for t in cell.findall('.//w:t',NS))
    span=cell.find('w:tcPr/w:gridSpan',NS);n=int(span.get('{'+NS['w']+'}val','1')) if span is not None else 1
    cells.append(text);cells.extend(['']*(n-1))
   out.append(cells)
  tables.append(out)
 return tables
def select_snapshot(snapshots,session):
 available=[s for s in snapshots if s['effective_session'] is not None and s['effective_session']<=session]
 if not available:return None
 return max(available,key=lambda s:s['effective_session'])

