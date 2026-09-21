"""Preview immutable saved-page imports; commit only reviewed content."""
from pathlib import Path
import hashlib
import secrets
import threading
import time
from tb_ra_form import prepare,commit_prepared,FILE
from tb_au_observer import read_strict

class FormImporter:
 def __init__(self,base):self.base=Path(base);self.pending={};self.lock=threading.Lock()
 def preview(self):
  folder=self.base/'recent_form';state=read_strict(self.base/'state'/FILE) or {}
  known={i['file_sha256'] for h in state.get('runners',{}).values() for i in h.get('imports',{}).values()}
  reports=[];prepared=[];seen=set();skipped=0
  for path in sorted(folder.iterdir()) if folder.exists() else []:
   if path.is_symlink() or not path.is_file() or path.suffix.lower() not in ('','.html','.htm'):continue
   if path.stat().st_size>2_000_000:reports.append({'file':path.name,'error':'File exceeds 2 MB'});continue
   try:
    digest=hashlib.sha256(path.read_text().encode()).hexdigest()
    if digest in known or digest in seen:skipped+=1;continue
    seen.add(digest)
    if len(prepared)>=20:reports.append({'file':path.name,'error':'Batch limit 20; import then scan again'});continue
    report,records,text,digest=prepare(self.base,path)
    reports.append({'file':path.name,**report});prepared.append((report,records,text,digest))
   except (ValueError,UnicodeError,OSError) as e:reports.append({'file':path.name,'error':str(e)})
  with self.lock:
   self.pending={k:v for k,v in self.pending.items() if v[0]>time.monotonic()-900}
   if len(self.pending)>=5:raise ValueError('Too many previews; retry later')
   token=secrets.token_urlsafe(24);self.pending[token]=(time.monotonic(),prepared)
  return {'preview_id':token,'files_ready':len(prepared),'skipped_files':skipped,'reports':reports}
 def commit(self,token):
  with self.lock:data=self.pending.pop(token,None)
  if not data or data[0]<time.monotonic()-900:raise ValueError('Preview expired; scan again')
  reports=[]
  for report,records,text,digest in data[1]:
   try:reports.append(commit_prepared(self.base,dict(report),records,text,digest))
   except (ValueError,OSError) as e:reports.append({'track':report['track'],'error':str(e)})
  return {'reports':reports,'imported_runners':sum(r.get('imported_runners',0) for r in reports),'database_updates':sum(r.get('database_updates',0) for r in reports)}
