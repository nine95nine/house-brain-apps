"""Exact app entrypoint; SIGTERM stops without an equipment action."""
import argparse
import json
import os
from pathlib import Path
from queue import Queue, Empty
import signal
from threading import Event, Thread
import time
import resource
from .runtime import Runtime
from .transport import HAClient
from .web import Ingress
from .installation import read_private, validate
from .interlock import safety_interlock


def cycle(runtime, client, config, now):
    """One live cycle. Kill-switch interlock (#56): analytics are ADVISORY, publication is MUTATION."""
    try:
        states=client.states()
    except Exception:
        states=None
    advisory=safety_interlock(states,"ADVISORY",now)
    publication=safety_interlock(states,"MUTATION",now)
    if states is None:
        runtime.failure(now,"DATA_OR_STORAGE_FAILURE")
    elif advisory!="ALLOW":
        runtime.hold(now,advisory)
    else:
        try:
            if not runtime.paused:
                runtime.backfill_one(now)
            runtime.tick(states,now)
        except Exception:
            runtime.failure(now,"DATA_OR_STORAGE_FAILURE")
    runtime.status={**runtime.status,"safety_interlock":{"advisory":advisory,"publication":publication}}
    try:
        if config.get("publish_status",False) and publication=="ALLOW":
            client.publish(runtime.status)
    except Exception:
        runtime.status={**runtime.status,"publication":"FAILED"}
    return runtime.status


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--options",default="/data/options.json")
    parser.add_argument("--data",default="/data")
    parser.add_argument("--replay",help="offline state snapshot JSON; no network and one cycle")
    args=parser.parse_args()
    config=json.loads(Path(args.options).read_text())
    # Development containment, not a measured production headroom claim.
    resource.setrlimit(resource.RLIMIT_AS,(256*1024*1024,256*1024*1024))
    if config.get('setup_only') is True:
        if args.replay:
            raise SystemExit('SETUP_REPLAY_REFUSED')
        from .provisioning import serve
        raise SystemExit(serve(args.data,Path(__file__).resolve().parent.parent))
    try:
        household=validate(read_private(Path(args.data)/"household.json"))
        if config.get("bindings") and config["bindings"]!=household["options"]["bindings"]:
            raise ValueError("OPTIONS_BUNDLE_BINDING_CONFLICT")
        config={**household["options"],**config,"bindings":household["options"]["bindings"]}
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit("SETUP_REQUIRED: valid private household.json bundle and matching options required")
    try:
        client=None if args.replay else HAClient(os.environ.get("SUPERVISOR_TOKEN"))
    except ValueError:
        raise SystemExit("HA_API_TOKEN_REQUIRED: Supervisor did not provide the Home Assistant API token")
    runtime=Runtime(config,args.data,household["register"],household["tariff"],client,
                    billing_policy=household["policy"],identity_register=household["parsed_register"],
                    adapters=household['adapters'],retained_bill=household.get('bill_packet'),
                    statements=household.get('statement_summary'))
    if args.replay:
        try:
            packet=json.loads(Path(args.replay).read_text())
            print(json.dumps(runtime.tick(packet["states"],packet["at"]),allow_nan=False))
        finally:
            runtime.close()
        return
    stop=Event()
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,lambda *_:stop.set())
    commands=Queue(maxsize=16)
    ingress=Ingress(lambda:runtime.status,commands)
    thread=Thread(target=ingress.run,daemon=True)
    thread.start()
    next_tick=0.
    try:
        while not stop.is_set():
            try:
                route,payload,done,result=commands.get_nowait()
                try:
                    if route=="/pause":
                        runtime.pause(payload["paused"])
                        runtime.status={**runtime.status,"state":"PAUSED" if runtime.paused else "RESUMING"}
                        result["paused"]=runtime.paused
                    elif route=="/model/rollback":
                        if set(payload)!={"role"}:
                            raise ValueError("ROLLBACK_ROLE_ONLY")
                        result.update(runtime.rollback_model(payload["role"]))
                    else:
                        result.update(runtime.identities.review(at=time.time(),**payload))
                except (ValueError,TypeError,KeyError):
                    result["error"]="REVIEW_OR_BINDING_INVALID"
                done.set()
            except Empty:
                pass
            if time.monotonic()>=next_tick:
                cycle(runtime,client,config,time.time())
                next_tick=time.monotonic()+60
            stop.wait(.25)
    finally:
        ingress.stop()
        runtime.close()


if __name__=="__main__":
    main()
