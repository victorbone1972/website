"""Local research prototype. Python 3.9+, standard library only."""
import json, sqlite3, secrets, mimetypes, re, uuid, threading
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
ROOT=Path(__file__).resolve().parent
DATA=ROOT/'data'; DATA.mkdir(exist_ok=True)
FILES=DATA/'attachments'; FILES.mkdir(exist_ok=True)
DB=DATA/'study.sqlite3'; TOKEN=secrets.token_urlsafe(32); LOCK=threading.Lock()
def conn():
 c=sqlite3.connect(DB);c.execute('PRAGMA foreign_keys=ON');return c
with conn() as c:
 c.executescript('''CREATE TABLE IF NOT EXISTS meta(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL);INSERT OR IGNORE INTO meta VALUES(1,0);
 CREATE TABLE IF NOT EXISTS patients(id TEXT PRIMARY KEY,data TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS visits(id TEXT PRIMARY KEY,patient TEXT NOT NULL REFERENCES patients(id),data TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS attachments(id TEXT PRIMARY KEY,patient TEXT NOT NULL REFERENCES patients(id),name TEXT,mime TEXT,path TEXT);
 CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at TEXT DEFAULT CURRENT_TIMESTAMP,action TEXT,detail TEXT);''')
def validate(s):
 if not isinstance(s,dict) or not isinstance(s.get('patients'),list) or not isinstance(s.get('visits'),list):raise ValueError('数据格式错误')
 if len(s['patients'])>10000 or len(s['visits'])>100000:raise ValueError('记录数量超限')
 ids=set();codes=set()
 for p in s['patients']:
  if not isinstance(p,dict) or not re.fullmatch(r'[\w-]{1,80}',str(p.get('id',''))):raise ValueError('患者ID错误')
  if p['id'] in ids:raise ValueError('患者ID重复')
  code=str(p.get('code','')).strip()
  if not code or code in codes:raise ValueError('研究编号为空或重复')
  ids.add(p['id']);codes.add(code)
  date.fromisoformat(p.get('date',''))
  if p.get('debond') and date.fromisoformat(p['debond'])<date.fromisoformat(p['date']):raise ValueError('拆除日期早于基线')
  if not 12<=int(p.get('age',0))<=17:raise ValueError('年龄须为12至17岁')
 vids=set()
 for v in s['visits']:
  if not isinstance(v,dict) or not re.fullmatch(r'[\w-]{1,80}',str(v.get('id',''))) or v['id'] in vids:raise ValueError('随访ID错误或重复')
  vids.add(v['id'])
  if v.get('patient') not in ids:raise ValueError('随访患者不存在')
  if v.get('status') not in ['草稿','待审核','已审核','需修正']:raise ValueError('审核状态错误')
  if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',str(v.get('date',''))):raise ValueError('随访日期错误')
  date.fromisoformat(v['date'])
  if v.get('status')=='已审核' and not str(v.get('reviewer','')).strip():raise ValueError('请填写审核者')
  for tooth in ['31','32','41','42']:
   row=v.get('teeth',{}).get(tooth,{})
   for k,lo,hi in [('gm',-10,15),('kg',0,20),('pd',0,15)]:
    z=row.get(k)
    if z not in [None,''] and not lo<=float(z)<=hi:raise ValueError('牙周数值超出允许范围')
 return s
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*a):pass
 def send(self,obj,status=200):
  b=json.dumps(obj,ensure_ascii=False).encode();self.bytes(b,'application/json; charset=utf-8',status)
 def bytes(self,b,mime,status=200):
  self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(b)
 def allowed(self):return self.headers.get('Host','') in ['127.0.0.1:8765','localhost:8765']
 def do_GET(self):
  if not self.allowed():return self.send({'error':'Host不允许'},403)
  u=urlparse(self.path)
  if u.path=='/api/state':
   with conn() as c:
    self.send({'revision':c.execute('SELECT revision FROM meta').fetchone()[0],'token':TOKEN,'patients':[json.loads(r[0]) for r in c.execute('SELECT data FROM patients')],'visits':[json.loads(r[0]) for r in c.execute('SELECT data FROM visits')]})
   return
  if u.path=='/api/attachments':
   p=parse_qs(u.query).get('patient',[''])[0]
   with conn() as c:self.send([{'id':r[0],'name':r[1],'mime':r[2]} for r in c.execute('SELECT id,name,mime FROM attachments WHERE patient=?',(p,))])
   return
  if u.path.startswith('/api/file/'):
   with conn() as c:r=c.execute('SELECT path,mime FROM attachments WHERE id=?',(u.path.split('/')[-1],)).fetchone()
   if not r:return self.send({'error':'附件不存在'},404)
   return self.bytes((FILES/r[0]).read_bytes(),r[1])
  if u.path=='/api/audit':
   with conn() as c:self.send([{'at':r[0],'action':r[1],'detail':r[2]} for r in c.execute('SELECT at,action,detail FROM audit ORDER BY id DESC LIMIT 100')])
   return
  f={'/':'index.html','/index.html':'index.html','/sw.js':'sw.js','/manifest.webmanifest':'manifest.webmanifest','/icon.svg':'icon.svg','/icon-192.png':'icon-192.png','/icon-512.png':'icon-512.png'}.get(u.path)
  if not f or not (ROOT/f).exists():return self.send({'error':'不存在'},404)
  return self.bytes((ROOT/f).read_bytes(),mimetypes.guess_type(f)[0] or 'application/octet-stream')
 def do_PUT(self):
  if not self.allowed() or self.headers.get('X-Study-Token')!=TOKEN:return self.send({'error':'请求校验失败，请刷新'},403)
  try:
   size=int(self.headers.get('Content-Length',0))
   if not 0<size<=10*1024*1024:return self.send({'error':'请求须小于10MB'},413)
   raw=self.rfile.read(size);u=urlparse(self.path)
   if u.path=='/api/state':
    payload=json.loads(raw);s=validate(payload)
    with LOCK,conn() as c:
     rev=c.execute('SELECT revision FROM meta').fetchone()[0]
     if payload.get('revision')!=rev:return self.send({'error':'数据已被其他页面修改，请刷新后重新操作'},409)
     oldp={r[0]:json.loads(r[1]) for r in c.execute('SELECT id,data FROM patients')};oldv={r[0]:json.loads(r[1]) for r in c.execute('SELECT id,data FROM visits')}
     newv={v['id']:v for v in s['visits']}
     for vid,old in oldv.items():
      if old.get('status')=='已审核' and newv.get(vid)!=old:raise ValueError('已审核记录不可修改或移除')
     current=set(oldp);new={p['id'] for p in s['patients']}
     if current-new:raise ValueError('测试版禁止删除患者，避免附件或记录丢失')
     for p in s['patients']:c.execute('INSERT OR REPLACE INTO patients(id,data) VALUES(?,?)',(p['id'],json.dumps(p,ensure_ascii=False))) if p['id'] not in current else c.execute('UPDATE patients SET data=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']))
     c.execute('DELETE FROM visits')
     for v in s['visits']:c.execute('INSERT INTO visits VALUES(?,?,?)',(v['id'],v['patient'],json.dumps(v,ensure_ascii=False)))
     changes={'revision':rev+1,'patients':[{'id':p['id'],'before':oldp.get(p['id']),'after':p} for p in s['patients'] if oldp.get(p['id'])!=p],'visits':[{'id':v['id'],'before':oldv.get(v['id']),'after':v} for v in s['visits'] if oldv.get(v['id'])!=v]}
     c.execute('UPDATE meta SET revision=revision+1');c.execute('INSERT INTO audit(action,detail) VALUES(?,?)',('保存',json.dumps(changes,ensure_ascii=False)))
    return self.send({'revision':rev+1})
   if u.path=='/api/attachments':
    q=parse_qs(u.query);patient=q.get('patient',[''])[0];name=Path(q.get('name',['file'])[0]).name[:160]
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):mime,ext='image/png','.png'
    elif raw.startswith(b'\xff\xd8\xff'):mime,ext='image/jpeg','.jpg'
    elif raw.startswith(b'%PDF-'):mime,ext='application/pdf','.pdf'
    else:raise ValueError('仅接受PNG、JPEG、PDF附件')
    uid=uuid.uuid4().hex
    with conn() as c:
     if not c.execute('SELECT 1 FROM patients WHERE id=?',(patient,)).fetchone():raise ValueError('患者不存在')
     dest=FILES/(uid+ext);dest.write_bytes(raw)
     try:
      c.execute('INSERT INTO attachments VALUES(?,?,?,?,?)',(uid,patient,name,mime,dest.name));c.execute('INSERT INTO audit(action,detail) VALUES(?,?)',('上传附件',patient+' / '+name))
     except Exception:dest.unlink(missing_ok=True);raise
    return self.send({'id':uid})
   return self.send({'error':'接口不存在'},404)
  except (ValueError,TypeError,KeyError,sqlite3.Error) as e:self.send({'error':str(e)},400)
if __name__=='__main__':
 print('OrthoGingiva 本机测试：http://127.0.0.1:8765',flush=True)
 print('仅监听本机；关闭服务后请备份整个 data 文件夹。Ctrl+C 停止。',flush=True)
 ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
