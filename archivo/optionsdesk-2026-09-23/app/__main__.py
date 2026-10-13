from __future__ import annotations
import argparse
import asyncio
import fcntl
import json
import os
import sys
from pathlib import Path
from app.config import ROOT, Config, load_config, database_path
from app.store import Store


class ProcessLock:
    def __init__(self,path): self.path=Path(path).with_suffix('.lock'); self.file=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+')
        try: fcntl.flock(self.file,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close(); raise RuntimeError('another engine owns this database')
        self.file.seek(0); self.file.truncate(); self.file.write(str(os.getpid())); self.file.flush()
        return self
    def __exit__(self,*args):
        fcntl.flock(self.file,fcntl.LOCK_UN);self.file.close()


async def main():
    parser=argparse.ArgumentParser(description='Local options research; defaults to OBSERVE')
    parser.add_argument('--config',type=Path,default=ROOT/'config.json')
    parser.add_argument('--db',type=Path,default=None)
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('init-db')
    doctor=sub.add_parser('doctor');doctor.add_argument('--probe-external',action='store_true')
    run=sub.add_parser('run');run.add_argument('--broker',choices=['fake','ibkr'],default='fake')
    run.add_argument('--max-seconds',type=float);run.add_argument('--simulated-analyst',action='store_true',help='fake only; deterministic proposals instead of OpenAI')
    rep=sub.add_parser('replay');rep.add_argument('fixture',type=Path);rep.add_argument('--db',type=Path,default=None)
    args=parser.parse_args()
    if args.command=='replay':
        from app.replay import replay
        print(json.dumps(await replay(args.fixture,args.db),sort_keys=True,indent=2));return 0
    config=load_config(args.config)
    with Store(args.db or database_path()) as store:
        if args.command=='init-db':
            store.set_kv('config',config.model_dump(mode='json'))
            print('Listo: SQLite schema 1');return 0
        if args.command=='doctor':
            from app.doctor import doctor
            report=await doctor(config,store,probe_external=args.probe_external)
            print(json.dumps(report,indent=2,ensure_ascii=False));return 0
        if store.kv('config'):
            config=Config.model_validate(store.kv('config'))
            if os.environ.get('OPENAI_MODEL'): config.openai_model=os.environ['OPENAI_MODEL']
        from app.analyst import OpenAIAnalyst,ScriptedAnalyst
        from app.engine import Engine
        if args.broker=='fake':
            from app.broker import FakeBroker
            broker=FakeBroker(config)
        else:
            if args.simulated_analyst: raise ValueError('simulated analyst allowed only with fake broker')
            from app.ibkr import IBKRBroker
            broker=IBKRBroker(config)
        analyst=ScriptedAnalyst(config) if args.simulated_analyst else OpenAIAnalyst(config)
        with ProcessLock(store.path):
            print(json.dumps({'mode':config.mode,'broker':args.broker,'simulated_analyst':args.simulated_analyst}),flush=True)
            await Engine(config,store,broker,analyst).run(args.max_seconds)
    return 0


if __name__=='__main__':
    try: sys.exit(asyncio.run(main()))
    except KeyboardInterrupt: print('Stopped; automatic execution disarmed')
    except Exception as exc:
        # Never print third-party exception bodies or environment values.
        print('Blocked: '+type(exc).__name__+'; run doctor and inspect local audit',file=sys.stderr)
        sys.exit(1)
