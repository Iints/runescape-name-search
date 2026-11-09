"""Persistent shortlist, scanner and browser-runner service for RuneScape names."""
from __future__ import annotations
import datetime as dt, json, re, sqlite3, threading, time, uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import hiscores_tool as core

def synchronized(fn):
    def wrapped(self,*args,**kwargs):
        with self.lock: return fn(self,*args,**kwargs)
    return wrapped

ROOT = Path(__file__).resolve().parent
COMMON_WORDS_PATH = ROOT / 'data' / 'common_words.tsv'
RANKING_VERSION = '2'
DB_PATH = ROOT / "data" / "hiscores.sqlite3"
GIVEN_NAMES = "aaron abby abe ada adam adrian aiden alex alice alina amara amy andrew angel anna aria arthur ashley atlas audrey ava bea bella ben blake brooke caleb camila carter charlie chloe claire clara colin cora daniel daphne david delia diana edith elena eli elias elise ella ellis emma eric ethan eva evelyn felix finn florence frank gabriel grace harper hazel henry iris isaac isaiah jack jacob james jane jasper jessica joan joseph julia julian kai karen katherine leo liam lily logan lucas lucy luna madeline maya mia miles naomi nathan nora oliver olivia oscar paige peter phoebe quinn rachel rebecca riley rose ruby samantha samuel sara scarlett sophia theo thomas violet william willow wyatt zoe".split()
SURNAMES = "adams allen anderson bailey baker barnes bell bennett brooks brown bryant butler campbell carter clark collins cook cooper cox cruz davis diaz edwards ellis evans floyd foster garcia gonzalez gray green griffin hall harris hayes henderson hill hughes jackson james jenkins johnson jones kennedy king lee lewis long lopez martin martinez mason miller mitchell moore morgan morris murphy nelson parker patel perry peterson phillips powell price ramirez reed richardson rivera roberts robinson rogers ross russell sanders scott simmons smith stewart taylor thomas thompson turner walker ward washington watson white williams wilson wood wright young".split()

class NameService:
    def __init__(self, db_path: str | Path = DB_PATH):
        self.db_path = Path(db_path)
        self.common_words = self._load_common_words()
        self.lock = threading.RLock()
        self.rate_lock = threading.Lock()
        self.last_request = 0.0
        self.scan_thread: threading.Thread | None = None
        self._init()

    @contextmanager
    def db(self):
        d=core.connect(self.db_path)
        d.executescript('''
        CREATE TABLE IF NOT EXISTS ui_batch(prefixes TEXT NOT NULL);
        INSERT INTO ui_batch(prefixes) SELECT '[]' WHERE NOT EXISTS(SELECT 1 FROM ui_batch);
        CREATE TABLE IF NOT EXISTS ui_candidates(id TEXT PRIMARY KEY, pinned INTEGER NOT NULL DEFAULT 0, skipped INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS ui_history(id TEXT PRIMARY KEY, status TEXT NOT NULL, verdict TEXT, checked_at TEXT, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ui_scan(id INTEGER PRIMARY KEY CHECK(id=1),status TEXT NOT NULL DEFAULT 'idle',started_at TEXT,finished_at TEXT,error TEXT,checked INTEGER NOT NULL DEFAULT 0,total INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO ui_scan(id,status) VALUES(1,'idle');
        CREATE TABLE IF NOT EXISTS ui_runner(id INTEGER PRIMARY KEY CHECK(id=1),status TEXT NOT NULL DEFAULT 'idle',runner_id TEXT,current_id TEXT,next_id TEXT,lease_id TEXT,lease_started REAL,lease_name TEXT,remaining_seconds INTEGER NOT NULL DEFAULT 300,remaining_attempts INTEGER NOT NULL DEFAULT 99,latest_verdict TEXT,session_start REAL,oneoff INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO ui_runner(id,status) VALUES(1,'idle');
        CREATE TABLE IF NOT EXISTS ui_runner_done(runner_id TEXT NOT NULL,candidate_id TEXT NOT NULL,lease_id TEXT NOT NULL,verdict TEXT NOT NULL,checked_at TEXT NOT NULL,PRIMARY KEY(runner_id,candidate_id,lease_id));
        CREATE TABLE IF NOT EXISTS ui_runner_queue(runner_id TEXT NOT NULL,seq INTEGER NOT NULL,candidate_id TEXT NOT NULL,PRIMARY KEY(runner_id,seq),UNIQUE(runner_id,candidate_id));
        CREATE TABLE IF NOT EXISTS ui_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        '''); d.commit()
        try: yield d
        except Exception:
            d.rollback(); raise
        else: d.commit()
        finally: d.close()

    def _init(self):
        self.db_path.parent.mkdir(parents=True,exist_ok=True)
        with self.db() as d:
            scan=d.execute('SELECT status FROM ui_scan WHERE id=1').fetchone()
            if scan and scan['status']=='running':
                d.execute("UPDATE ui_scan SET status='interrupted',finished_at=?,error='Server stopped during scan; start again to resume from saved checks' WHERE id=1",(self.now(),))
            version=d.execute("SELECT value FROM ui_meta WHERE key='ranking_version'").fetchone()
            if not version or version[0]!=RANKING_VERSION:
                runner=d.execute('SELECT runner_id FROM ui_runner WHERE id=1').fetchone()
                if runner and runner['runner_id']:
                    rid=runner['runner_id']
                    d.execute('DELETE FROM ui_runner_queue WHERE runner_id=?',(rid,))
                    for seq,row in enumerate(self._eligible_rows(d)):
                        d.execute('INSERT INTO ui_runner_queue(runner_id,seq,candidate_id) VALUES(?,?,?)',(rid,seq,row['canonical']))
                d.execute("INSERT INTO ui_meta(key,value) VALUES('ranking_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(RANKING_VERSION,))

    @staticmethod
    def _load_common_words() -> dict[str,float]:
        if not COMMON_WORDS_PATH.exists():
            raise FileNotFoundError(f'Missing common-word filter: {COMMON_WORDS_PATH}')
        scores={}
        for line in COMMON_WORDS_PATH.read_text(encoding='utf-8').splitlines():
            if not line or line.startswith('#'): continue
            word,score=line.split('\t',1)
            scores[word]=float(score)
        return scores

    @staticmethod
    def now(): return core.utc_now()

    @staticmethod
    def canonical(name: str):
        if not isinstance(name,str): return None
        return core.canonicalize(name)

    def _ensure(self,d,name):
        cid=self.canonical(name)
        if not cid: raise ValueError('Invalid RuneScape name')
        core.merge_candidate(d,name,'manual lookup')
        d.execute('INSERT OR IGNORE INTO ui_candidates(id) VALUES(?)',(cid,)); return cid

    def _check(self,d,cid,game):
        # Keep all UI-triggered Hiscores traffic serialized with a one-second minimum gap.
        with self.rate_lock:
            delay=1.0-(time.monotonic()-self.last_request)
            if delay>0: time.sleep(delay)
            previous=d.execute('SELECT * FROM checks WHERE canonical=? AND game=?',(cid,game)).fetchone()
            try:
                outcome=core.check_candidate(d,cid,game,interval=1,retries=1)
                if outcome['result']=='unknown' and previous and previous['result'] in ('listed','not_listed'):
                    d.execute('''UPDATE checks SET result=?,http_status=?,content_type=?,schema_valid=?,overall_level=?,checked_at=?,attempts=?,error=?
                                 WHERE canonical=? AND game=?''',
                              (previous['result'],previous['http_status'],previous['content_type'],previous['schema_valid'],
                               previous['overall_level'],previous['checked_at'],previous['attempts'],previous['error'],cid,game))
                return outcome
            finally: self.last_request=time.monotonic()

    def batch(self,prefixes=None):
        with self.db() as d:
            if prefixes is not None:
                if not isinstance(prefixes,list) or any(not isinstance(p,str) or len(p)!=1 or not p.isascii() or not p.isalpha() for p in prefixes): raise ValueError('prefixes must be single ASCII letters')
                prefixes=list(dict.fromkeys(p.upper() for p in prefixes))
                d.execute('UPDATE ui_batch SET prefixes=?',(json.dumps(prefixes),))
                self._import_prefixes(d,prefixes)
            row=d.execute('SELECT prefixes FROM ui_batch').fetchone(); return json.loads(row[0])

    def _import_prefixes(self,d,prefixes):
        """Expand selected letters from bundled wordlists and deterministic short names."""
        wanted={p.casefold() for p in prefixes}
        for p in prefixes:
            for name in [p]+[p+a for a in core.ALNUM]: core.merge_candidate(d,name,'generated:one-two-character',generated=True)
        for n in GIVEN_NAMES:
            if n[0] in wanted: core.merge_candidate(d,n,'curated:given-name',person_tag=True)
        for n in SURNAMES:
            if n[0] in wanted: core.merge_candidate(d,n,'curated:surname',person_tag=True)
        for word in self.common_words:
            if word[0] in wanted:
                core.merge_candidate(d,word,'common-word:wordfreq-3.1.1',word_tag=True)
        # Preserve existing user priority seeds as favorites and manual ordering.
        favorite_file=ROOT/'data'/'a_priority.txt'
        if favorite_file.exists():
            for name in favorite_file.read_text(encoding='utf-8-sig').splitlines():
                cid=core.canonicalize(name)
                if cid and cid[0] in wanted:
                    core.merge_candidate(d,name,'manual priority seed')
                    inserted=d.execute('INSERT OR IGNORE INTO ui_candidates(id) VALUES(?)',(cid,)).rowcount
                    if inserted: d.execute('UPDATE ui_candidates SET priority=100 WHERE id=?',(cid,))
        d.commit()

    def _eligible_rows(self,d,include_skipped=False,require_verified=True):
        batchrow=d.execute('SELECT prefixes FROM ui_batch').fetchone()
        prefixes=json.loads(batchrow[0]) if batchrow else []
        if not prefixes:return []
        rows=d.execute('''SELECT c.*,coalesce(u.pinned,0) pinned,coalesce(u.skipped,0) skipped,coalesce(u.priority,0) priority,
          o.result osrs, o.checked_at osrs_at,r.result rs3,r.checked_at rs3_at,h.status history_status,h.verdict history_verdict,h.checked_at history_at
          FROM candidates c LEFT JOIN ui_candidates u ON u.id=c.canonical
          LEFT JOIN checks o ON o.canonical=c.canonical AND o.game='osrs' LEFT JOIN checks r ON r.canonical=c.canonical AND r.game='rs3'
          LEFT JOIN ui_history h ON h.id=c.canonical''').fetchall()
        out=[]
        for x in rows:
            if x['canonical'][0].upper() not in prefixes or (x['skipped'] and not include_skipped) or x['history_status'] in ('unavailable','available'): continue
            commonness=self.common_words.get(x['canonical'],0.0)
            if x['char_length']>=3 and not x['person_tag'] and not commonness and not x['pinned']: continue
            # Every length is a tier: 1, 2, 3, ... 12. People and common words
            # share a tier; user priority and then frequency decide within it.
            category=x['char_length']
            try:
                evidence_age=min(dt.datetime.fromisoformat(x[k]).timestamp() for k in ('osrs_at','rs3_at') if x[k])
            except ValueError:
                evidence_age=0
            rank=(0 if x['pinned'] else category, -x['priority'], category, -int(bool(x['person_tag'])), -commonness, -evidence_age, x['canonical'])
            if require_verified and not x['pinned'] and not (x['osrs']=='not_listed' and x['rs3']=='not_listed'): continue
            out.append((rank,x))
        out.sort(key=lambda pair:pair[0]); return [x for _,x in out]

    def _row(self,d,x,rank):
        sources=json.loads(x['sources_json'])
        word_score=self.common_words.get(x['canonical'])
        return {'id':x['canonical'],'name':x['display_name'],'rank':rank,'reason':'pinned' if x['pinned'] else 'short name' if x['char_length']<=2 else 'surname' if 'curated:surname' in sources else 'person' if x['person_tag'] else f'common word · Zipf {word_score:.2f}' if word_score else 'manual',
          'pinned':bool(x['pinned']),'skipped':bool(x['skipped']),'priority':x['priority'],
          'public':{'osrs':{'result':x['osrs'] or 'unchecked','checked_at':x['osrs_at']},'rs3':{'result':x['rs3'] or 'unchecked','checked_at':x['rs3_at']}},
          'history':{'status':x['history_status'],'verdict':x['history_verdict'],'checked_at':x['history_at']}}

    def state(self):
        with self.db() as d:
            batch=self.batch(); scan=d.execute('SELECT * FROM ui_scan WHERE id=1').fetchone(); r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            allrows=self._eligible_rows(d,include_skipped=True)
            shortlist=[self._row(d,x,i+1) for i,x in enumerate(x for x in allrows if not x['skipped'])]
            skipped=[self._row(d,x,i+1) for i,x in enumerate(x for x in allrows if x['skipped'])]
            history=[dict(x) for x in d.execute('SELECT h.id,c.display_name name,h.status,h.verdict,h.checked_at,h.updated_at FROM ui_history h LEFT JOIN candidates c ON c.canonical=h.id ORDER BY h.updated_at DESC LIMIT 200')]
            currentrow=d.execute('SELECT canonical id,display_name name FROM candidates WHERE canonical=?',(r['current_id'],)).fetchone() if r['current_id'] else None
            nextrow=d.execute('SELECT canonical id,display_name name FROM candidates WHERE canonical=?',(r['next_id'],)).fetchone() if r['next_id'] else None
            latest=d.execute('SELECT c.display_name name,v.verdict,v.checked_at FROM ui_runner_done v LEFT JOIN candidates c ON c.canonical=v.candidate_id WHERE v.runner_id=? ORDER BY v.rowid DESC LIMIT 1',(r['runner_id'],)).fetchone() if r['runner_id'] else None
            runner={'status':r['status'],'current':dict(currentrow) if currentrow else None,'next':dict(nextrow) if nextrow else None,'remaining_seconds':r['remaining_seconds'],'remaining_attempts':r['remaining_attempts'],'latest_verdict':dict(latest) if latest else None,'runner_id':r['runner_id'],'lease_id':r['lease_id']}
            if r['session_start']:
                runner['remaining_seconds']=max(0,r['remaining_seconds']-(time.time()-r['session_start']))
            return {'batch':{'prefixes':batch},'scan':{'status':scan['status'],'started_at':scan['started_at'],'finished_at':scan['finished_at'],'error':scan['error'],'checked':scan['checked'],'total':scan['total']},'shortlist':shortlist,'skipped':skipped,'history':history,'runner':runner}

    def set_candidate(self,name,field,value):
        with self.db() as d:
            cid=self._ensure(d,name)
            if field=='pinned': d.execute('UPDATE ui_candidates SET pinned=? WHERE id=?',(int(bool(value)),cid))
            elif field=='skipped': d.execute('UPDATE ui_candidates SET skipped=? WHERE id=?',(int(bool(value)),cid))
            elif field=='priority': d.execute('UPDATE ui_candidates SET priority=? WHERE id=?',(max(-10000,min(10000,int(value))),cid))
            elif field=='front': d.execute('UPDATE ui_candidates SET priority=10000 WHERE id=?',(cid,))
            return cid

    def reset_history(self,name):
        with self.db() as d:
            cid=self.canonical(name)
            if not cid: raise ValueError('Invalid candidate id')
            d.execute('DELETE FROM ui_history WHERE id=?',(cid,))

    def lookup(self,name):
        cid=self.canonical(name)
        if not cid: raise ValueError('Invalid RuneScape name')
        with self.db() as d:
            self._ensure(d,name)
            outcome=self._check(d,cid,'osrs')
            if outcome['result']=='not_listed': outcome=self._check(d,cid,'rs3')
            return {'id':cid,'name':name,'result':outcome['result'],'game':outcome['game'],'checked_at':outcome['checked_at'],'error':outcome.get('error')}

    def start_scan(self):
        with self.db() as d:
            if self.scan_thread and self.scan_thread.is_alive(): return {'status':'running'}
            # Include unchecked names in ranked order so the top candidates are checked first.
            ids=[x['canonical'] for x in self._eligible_rows(d,require_verified=False)]
            if not json.loads(d.execute('SELECT prefixes FROM ui_batch').fetchone()[0]):
                raise ValueError('Save at least one batch letter before starting a scan')
            d.execute("UPDATE ui_scan SET status='running',started_at=?,finished_at=NULL,error=NULL,checked=0,total=? WHERE id=1",(self.now(),len(ids)))
        def work():
            checked=0; err=None
            try:
                for cid in ids:
                    with self.db() as d:
                        osrs=d.execute("SELECT result FROM checks WHERE canonical=? AND game='osrs'",(cid,)).fetchone()
                        old=d.execute("SELECT checked_at FROM checks WHERE canonical=? AND game='osrs'",(cid,)).fetchone()
                        fresh=old and (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(old[0])).total_seconds()<86400
                        if osrs and osrs[0] in ('listed','not_listed') and fresh:
                            if osrs[0]=='not_listed':
                                rs3=d.execute("SELECT result,checked_at FROM checks WHERE canonical=? AND game='rs3'",(cid,)).fetchone()
                                rs3fresh=rs3 and (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(rs3['checked_at'])).total_seconds()<86400
                                if not rs3 or rs3['result'] not in ('listed','not_listed') or not rs3fresh: self._check(d,cid,'rs3')
                            checked+=1
                            d.execute('UPDATE ui_scan SET checked=? WHERE id=1',(checked,))
                            continue
                        self._check(d,cid,'osrs')
                        osrs=d.execute("SELECT result FROM checks WHERE canonical=? AND game='osrs'",(cid,)).fetchone()
                        if osrs and osrs[0]=='not_listed': self._check(d,cid,'rs3')
                    checked+=1
                    with self.db() as d:d.execute('UPDATE ui_scan SET checked=? WHERE id=1',(checked,))
            except Exception as e: err=f'{type(e).__name__}: {e}'
            with self.db() as d:d.execute('UPDATE ui_scan SET status=?,finished_at=?,error=?,checked=? WHERE id=1',('failed' if err else 'finished',self.now(),err,checked))
        self.scan_thread=threading.Thread(target=work,daemon=True); self.scan_thread.start(); return {'status':'running'}

    @synchronized
    def runner_start(self,rid):
        if not rid: raise ValueError('runner_id required')
        with self.db() as d:
            r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if r['runner_id']==rid and r['status'] in ('running','paused'):
                remaining=max(0,r['remaining_seconds']-(time.time()-r['session_start'])) if r['session_start'] else r['remaining_seconds']
                if remaining<=0 or r['remaining_attempts']<=0:
                    d.execute("UPDATE ui_runner SET status='expired' WHERE id=1")
                    return {'status':'expired'}
                return {'status':r['status']}
            if r['runner_id']==rid and r['status']=='stopped':
                remaining=max(0,r['remaining_seconds']-(time.time()-r['session_start'])) if r['session_start'] else r['remaining_seconds']
                if remaining<=0 or r['remaining_attempts']<=0:
                    d.execute("UPDATE ui_runner SET status='expired' WHERE id=1")
                    return {'status':'expired'}
                d.execute("UPDATE ui_runner SET status='running' WHERE id=1")
                return {'status':'running'}
            if r['status'] in ('running','paused') and r['runner_id'] and r['runner_id']!=rid: raise ValueError('Another runner session is active')
            resume_id=r['current_id'] if r['lease_id'] else None
            d.execute('DELETE FROM ui_runner_queue WHERE runner_id=?',(rid,))
            for i,x in enumerate(self._eligible_rows(d)): d.execute('INSERT INTO ui_runner_queue VALUES(?,?,?)',(rid,i,x['canonical']))
            d.execute("UPDATE ui_runner SET runner_id=?,status='running',remaining_seconds=300,remaining_attempts=99,session_start=?,current_id=NULL,next_id=?,lease_id=NULL,latest_verdict=NULL,oneoff=0 WHERE id=1",(rid,time.time(),resume_id))
            return {'status':'running'}

    @synchronized
    def runner_action(self,action):
        with self.db() as d:
            r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if action=='pause': d.execute("UPDATE ui_runner SET status='paused' WHERE id=1")
            elif action=='resume': d.execute("UPDATE ui_runner SET status='running' WHERE id=1")
            else:d.execute("UPDATE ui_runner SET status='stopped' WHERE id=1")
            return {'status':d.execute('SELECT status FROM ui_runner WHERE id=1').fetchone()[0]}

    @synchronized
    def runner_oneoff(self,rid,name=None,candidate_id=None,defer_pending=False):
        if not rid: raise ValueError('runner_id required')
        candidate_id=self.canonical(candidate_id or name or '')
        if not candidate_id: raise ValueError('name or candidate_id required')
        with self.db() as d:
            current=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if current['runner_id'] and current['runner_id']!=rid and current['status'] in ('running','paused'): raise ValueError('Another runner session is active')
            row=d.execute('SELECT display_name FROM candidates WHERE canonical=?',(candidate_id,)).fetchone()
            if not row:
                candidate_id=self._ensure(d,name or candidate_id)
                row=d.execute('SELECT display_name FROM candidates WHERE canonical=?',(candidate_id,)).fetchone()
            if not row: raise ValueError('Unknown candidate')
            hist=d.execute('SELECT status FROM ui_history WHERE id=?',(candidate_id,)).fetchone()
            if hist and hist['status']=='unavailable': raise ValueError('Unavailable candidate must be reset before retrying')
            if current['lease_id'] and current['current_id']==candidate_id:
                return {'status':'leased','candidate':{'id':candidate_id,'name':current['lease_name']},
                        'lease_id':current['lease_id'],'remaining_seconds':max(0,current['remaining_seconds']-(time.time()-(current['session_start'] or time.time()))),
                        'remaining_attempts':current['remaining_attempts']}
            if current['lease_id'] and current['current_id']!=candidate_id and not defer_pending:
                d.execute('UPDATE ui_runner SET next_id=?,oneoff=1 WHERE id=1',(candidate_id,))
                return {'status':'queued','candidate_id':candidate_id}
            if current['lease_id'] and current['current_id']!=candidate_id:
                # The browser has no active check; keep the interrupted candidate pending.
                # It remains in the snapshot queue and will be selected after this one-off.
                previous_id=current['current_id']
                if not d.execute('SELECT 1 FROM ui_runner_queue WHERE runner_id=? AND candidate_id=?',(rid,previous_id)).fetchone():
                    first=d.execute('SELECT min(seq) FROM ui_runner_queue WHERE runner_id=?',(rid,)).fetchone()[0]
                    d.execute('INSERT INTO ui_runner_queue(runner_id,seq,candidate_id) VALUES(?,?,?)',(rid,(first or 0)-1,previous_id))
                d.execute('UPDATE ui_runner SET current_id=NULL,lease_id=NULL,lease_started=NULL,lease_name=NULL WHERE id=1')
            d.execute("UPDATE ui_runner SET runner_id=?,status='running',session_start=coalesce(session_start,?),next_id=?,oneoff=1 WHERE id=1",(rid,time.time(),candidate_id))
        return self.runner_next(rid)

    @synchronized
    def runner_next(self,rid):
        with self.db() as d:
            r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if r['runner_id']!=rid: raise ValueError('Unknown runner session')
            remain=max(0,r['remaining_seconds']-(time.time()-r['session_start'])) if r['session_start'] else r['remaining_seconds']
            if r['status']!='running' or remain<=0 or r['remaining_attempts']<=0:
                expired=remain<=0 or r['remaining_attempts']<=0
                if expired: d.execute("UPDATE ui_runner SET status='expired' WHERE id=1")
                return {'candidate':None,'lease_id':None,'remaining_seconds':remain,'remaining_attempts':r['remaining_attempts'],'status':'expired' if expired else r['status']}
            if r['lease_id']:
                return {'candidate':{'id':r['current_id'],'name':r['lease_name']},'lease_id':r['lease_id'],'remaining_seconds':remain,'remaining_attempts':r['remaining_attempts'],'status':'leased'}
            candidate_id=r['next_id']
            if not candidate_id:
                done={z[0] for z in d.execute('SELECT candidate_id FROM ui_runner_done WHERE runner_id=?',(rid,))}
                x=d.execute('SELECT candidate_id FROM ui_runner_queue WHERE runner_id=? ORDER BY seq',(rid,)).fetchall()
                candidate_id=next((z[0] for z in x if z[0] not in done),None)
                # Front is an explicit, live override; it takes precedence over the saved queue.
                if candidate_id:
                    front=d.execute("SELECT u.id FROM ui_candidates u LEFT JOIN ui_history h ON h.id=u.id WHERE u.priority>=10000 AND u.id NOT IN (SELECT candidate_id FROM ui_runner_done WHERE runner_id=?) AND coalesce(h.status,'') NOT IN ('unavailable','available') ORDER BY u.priority DESC,u.id LIMIT 1",(rid,)).fetchone()
                    if front: candidate_id=front[0]
            if not candidate_id: return {'candidate':None,'lease_id':None,'remaining_seconds':remain,'remaining_attempts':r['remaining_attempts'],'status':'empty'}
            row=d.execute('SELECT display_name FROM candidates WHERE canonical=?',(candidate_id,)).fetchone()
            lease=uuid.uuid4().hex
            d.execute('UPDATE ui_runner SET current_id=?,next_id=NULL,lease_id=?,lease_started=?,lease_name=? WHERE id=1',(candidate_id,lease,time.time(),row['display_name'] if row else candidate_id))
            return {'candidate':{'id':candidate_id,'name':row['display_name'] if row else candidate_id},'lease_id':lease,'remaining_seconds':remain,'remaining_attempts':r['remaining_attempts'],'status':'leased'}

    @synchronized
    def runner_verdict(self,data):
        rid,cid,lid=data.get('runner_id'),self.canonical(data.get('candidate_id','')),data.get('lease_id'); verdict=data.get('verdict')
        if verdict not in ('available','unavailable') or not rid or not cid or not lid: raise ValueError('runner_id, candidate_id, lease_id and valid verdict required')
        checked=data.get('checked_at') or self.now()
        with self.db() as d:
            prior=d.execute('SELECT verdict FROM ui_runner_done WHERE runner_id=? AND candidate_id=? AND lease_id=?',(rid,cid,lid)).fetchone()
            if prior:return {'status':'accepted','verdict':prior[0],'duplicate':True}
            r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if r['runner_id']!=rid or r['current_id']!=cid or r['lease_id']!=lid: raise ValueError('Lease does not match active candidate')
            d.execute('INSERT INTO ui_runner_done VALUES(?,?,?,?,?)',(rid,cid,lid,verdict,checked))
            d.execute("INSERT INTO ui_history VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,verdict=excluded.verdict,checked_at=excluded.checked_at,updated_at=excluded.updated_at",(cid,verdict,verdict,checked,self.now()))
            elapsed=time.time()-(r['session_start'] or time.time())
            remaining_value=data.get('remaining_seconds')
            attempts_value=data.get('remaining_attempts')
            estimated_seconds=max(0,r['remaining_seconds']-elapsed)
            seconds=min(estimated_seconds,max(0,float(remaining_value))) if remaining_value is not None else estimated_seconds
            attempts=min(max(0,r['remaining_attempts']-1),max(0,int(attempts_value))) if attempts_value is not None else max(0,r['remaining_attempts']-1)
            d.execute('UPDATE ui_runner SET status=?,current_id=NULL,lease_id=NULL,lease_started=NULL,lease_name=NULL,remaining_seconds=?,remaining_attempts=?,session_start=?,latest_verdict=? WHERE id=1',('paused' if verdict=='available' else r['status'],seconds,attempts,time.time(),verdict))
            return {'status':'accepted','verdict':verdict,'duplicate':False}

    @synchronized
    def runner_progress(self,data):
        with self.db() as d:
            r=d.execute('SELECT * FROM ui_runner WHERE id=1').fetchone()
            if data.get('runner_id') and data['runner_id']!=r['runner_id']: raise ValueError('Unknown runner session')
            sets=[]; vals=[]
            for key,col in [('remaining_seconds','remaining_seconds'),('remaining_attempts','remaining_attempts')]:
                if key in data and data[key] is not None:
                    observed=max(0,float(data[key])) if key=='remaining_seconds' else max(0,int(data[key]))
                    if key=='remaining_seconds' and r['session_start']:
                        observed=min(observed,max(0,r[key]-(time.time()-r['session_start'])))
                    else: observed=min(observed,r[key])
                    sets.append(col+'=?'); vals.append(observed)
            if sets: d.execute('UPDATE ui_runner SET '+','.join(sets)+' WHERE id=1',vals)
            if data.get('remaining_seconds') is not None: d.execute('UPDATE ui_runner SET session_start=? WHERE id=1',(time.time(),))
            return {'status':r['status']}
