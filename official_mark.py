"""Deterministic HoYoLAB mark/unmark verification. No LLM or screen matching.

Uses a dedicated browser profile and the account's ordinary same-site session.
Never reads/export cookies or changes any marker other than the requested ID.
"""
import argparse
import asyncio
import hashlib
import json
import os
import math
import time
import sys
from urllib.request import Request, urlopen
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Error as PlaywrightError

ROOT = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parent
STATE = ROOT / "official-mark-state.json"
RESULT = ROOT / "official-mark-result.json"
API = "https://sg-public-api.hoyolab.com/common/map_user/ys_obc/v1/map/point/"
MAP = "https://act.hoyolab.com/ys/app/interactive-map/index.html?lang=ko-kr"
# Official regional investigation containers, not generic Mora investigation pins.
MORA_LABEL_IDS = frozenset((129,359,366,596,329,723,819,838))


def collectible_kind(name, category=None, label_id=None):
    name = ' '.join(name.lower().split())
    if label_id in MORA_LABEL_IDS or 'mora chest' in name or ('모라' in name and '상자' in name):
        return 'mora'
    if name.endswith('oculus'):
        return 'oculus'
    if category in (13,186):
        return 'chest'
    if 'chest' in name:
        return 'chest'
    return None


def catalog_get(map_id,route):
    base='https://sg-public-api-static.hoyolab.com/common/map_user/ys_obc'
    url=base+route+f'&map_id={map_id}&app_sn=ys_obc&lang=en-us'
    with urlopen(Request(url,headers={'Origin':'https://act.hoyolab.com','Referer':'https://act.hoyolab.com/'}),timeout=30) as response:
        body=json.load(response)
    if body.get('retcode')!=0:
        raise RuntimeError('공식 수집품 목록 조회 실패')
    return body['data']


def classified_labels(tree):
    labels={}
    categories=set()
    def walk(node, category):
        children=node.get('children') or []
        if children:
            for child in children:
                walk(child,category)
        else:
            kind=collectible_kind(node.get('name',''),category,int(node['id']))
            if kind:
                labels[int(node['id'])]=kind
                categories.add(category)
    for node in tree:
        walk(node,int(node['id']))
    if not labels:
        raise RuntimeError('수집품 종류 분류 실패')
    return labels,categories


def fetch_catalog(map_id):
    labels,categories=classified_labels(catalog_get(map_id,'/v2/map/label/tree?')['tree'])
    points={}
    for category in sorted(categories):
        data=catalog_get(map_id,'/v3/map/point/list?label_ids='+str(category))
        for point in data['point_list']:
            label=int(point['label_id'])
            sign=point.get('icon_sign')
            if label not in labels or not isinstance(sign,(int,float)):
                continue
            x,y=float(point['x_pos']),float(point['y_pos'])
            if not math.isfinite(x) or not math.isfinite(y):
                continue
            points[int(point['id'])]={'id':int(point['id']),'label_id':label,'kind':labels[label],
                'map':map_id,'x':x,'y':y,'underground':bool(sign)}
    return list(points.values())


def nearest_collectible(points, location, kind, excluded, maximum):
    if not math.isfinite(maximum) or maximum<=0:
        raise ValueError('거리 임계값은 양수여야 합니다.')
    x,y=float(location['lng']),float(location['lat'])
    if not math.isfinite(x) or not math.isfinite(y) or kind not in ('oculus','chest','mora'):
        raise ValueError('잘못된 위치 또는 수집 종류')
    underground=int(location['floor'])>0
    nearby=[]
    for point in points:
        if point['map']!=location['map'] or point['kind']!=kind:
            continue
        distance=math.hypot(point['x']-x,point['y']-y)
        nearby.append((distance,point['id'],point))
    # A surface map image can remain visible while the player is underground.
    # Use the opposite plane only if no pin on the map-derived plane is in range.
    # Collected/reserved pins still count as plane evidence, preventing a second
    # capture from switching planes just because the first pin was marked.
    preferred_near=any(d<=maximum and p['underground']==underground for d,_,p in nearby)
    opposite_near=any(d<=maximum and p['underground']!=underground for d,_,p in nearby)
    fallback=not preferred_near and opposite_near
    selected_plane=not underground if fallback else underground
    candidates=[(d,id,p) for d,id,p in nearby if p['underground']==selected_plane and id not in excluded]
    if not candidates:
        return None,'같은 종류·지상/지하의 미획득 후보 없음'
    distance,_,point=min(candidates,key=lambda item:(item[0],item[1]))
    if distance>maximum:
        return None,f'가장 가까운 후보 {distance:.2f} > 거리 제한 {maximum:g}'
    return dict(point,distance=distance,map_underground=underground,
                plane_selection='nearby-point-fallback' if fallback else 'map-floor'),None


class RegistrationSession:
    """One browser on the existing sequential worker; targets live in capture JSON."""
    def __init__(self):
        self.runtime=None
        self.browser=None
        self.page=None
        self.catalogs={}
        self.visible=False

    async def open(self, visible=False):
        if self.page and not self.page.is_closed():
            try:
                # A queued browser-close event may not have run while the worker
                # was waiting for its next job. Check the transport before reuse.
                await self.page.evaluate('1')
                if not visible or self.visible:
                    if visible: await self.page.bring_to_front()
                    return
            except PlaywrightError:
                pass
        shown=''
        if visible:
            tree=await asyncio.to_thread(catalog_get,2,'/v2/map/label/tree?')
            labels,_=classified_labels(tree['tree'])
            shown=','.join(str(label) for label in sorted(labels))
        await self.close()
        self.runtime=await async_playwright().start()
        self.browser=await self.runtime.chromium.launch_persistent_context(
            str(ROOT/'official-browser-profile'),channel='chrome',headless=not visible,
            viewport={'width':1280,'height':900})
        self.visible=visible
        self.page=self.browser.pages[0] if self.browser.pages else await self.browser.new_page()
        await self.page.goto(MAP+f'#/map/2?shown_types={shown}&zoom=-3.00',wait_until='domcontentloaded',timeout=60000)

    async def close(self):
        browser,runtime=self.browser,self.runtime
        self.runtime=self.browser=self.page=None
        self.visible=False
        try:
            if browser:
                try: await browser.close()
                except PlaywrightError: pass
        finally:
            if runtime:
                try: await runtime.stop()
                except PlaywrightError: pass

    async def ensure_login(self, stop, timeout=600):
        """Read-only sign-in check; never treats a visible map as authentication."""
        await self.open(visible=True)
        deadline=asyncio.get_running_loop().time()+timeout
        while not stop.is_set():
            if self.page is None or self.page.is_closed():
                raise RuntimeError('지도 창이 닫혔습니다. 지도 열기를 눌러 로그인을 완료하세요.')
            try:
                await OfficialMap(self.page,2).read()
                return
            except (RuntimeError,PlaywrightError):
                if asyncio.get_running_loop().time()>=deadline:
                    raise RuntimeError('로그인을 확인하지 못했습니다. 지도 열기를 눌러 다시 시도하세요.')
                await asyncio.sleep(1)
        raise asyncio.CancelledError()

    async def points(self, map_id):
        cached=self.catalogs.get(map_id)
        if not cached or time.monotonic()-cached[0]>600:
            self.catalogs[map_id]=(time.monotonic(),await asyncio.to_thread(fetch_catalog,map_id))
        return self.catalogs[map_id][1]

    async def register(self, file, read_record, write_record, reserved):
        record=read_record(file)
        state=record.get('registration',{})
        if record.get('queue_state') in ('held','deleted'):
            return {'status':'ignored','reason':'큐에서 보관 또는 삭제한 항목'}
        if state.get('status')=='marked':
            return state
        if not record.get('register_requested'):
            return {'status':'ignored','reason':'이전 시험 캡처는 자동 등록하지 않음'}
        try:
            location=record['location']
            kind=record['kind']
            maximum=float(state['max_distance'])
            await self.open()
            client=OfficialMap(self.page,int(location['map']))
            target=state.get('target')
            if target is None:
                marked=await client.read()
                points=await self.points(int(location['map']))
                target,reason=nearest_collectible(points,location,kind,
                    marked|reserved(file,int(location['map'])),maximum)
                if target is None:
                    state.update(status='unregistered',reason=reason)
                    write_record(file,{'registration':state})
                    return state
                state.update(target=target,status='sending',reason='저장 확인 중')
                # Persist the immutable target before the first possible POST.
                write_record(file,{'registration':state})
            else:
                # Resumption may confirm a previous ambiguous request, but never
                # replace its target with another nearby collectible.
                if target['map']!=location['map'] or target['kind']!=kind:
                    raise ValueError('저장된 대상과 캡처 종류/지도가 다릅니다.')
                if target['distance']>maximum:
                    if target['id'] not in await client.read():
                        raise ValueError(f'고정된 대상 거리 {target["distance"]:.2f} > 거리 제한 {maximum:g}')
            outcome=await client.set_mark(target['id'],True)
            state.update(status='marked',reason='공식 지도 저장 확인',outcome=outcome)
        except Exception as exc:
            state.update(status='unregistered',reason=str(exc))
        write_record(file,{'registration':state})
        return state


def save(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def point_ids(reply):
    if reply.get("retcode") != 0:
        raise RuntimeError(f"Official API retcode={reply.get('retcode')}: {reply.get('message', '')}")
    rows = reply.get("data", {}).get("list")
    if not isinstance(rows, list):
        raise RuntimeError("Unknown mark-list schema; refusing to write")
    if any(not isinstance(row, dict) or not isinstance(row.get("point_id"), (int, str)) for row in rows):
        raise RuntimeError("Unknown mark-list entries; refusing to write")
    return {int(row["point_id"]) for row in rows}


def digest(ids):
    return hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()


class OfficialMap:
    def __init__(self, page, map_id):
        self.page, self.map_id = page, map_id

    async def request(self, action, point=None):
        params = {"map_id": self.map_id, "app_sn": "ys_obc", "lang": "ko-kr"}
        if point is not None:
            params["point_id"] = point
        return await self.page.evaluate("""async ({url, params, write}) => {
            const controller = new AbortController();
            const timeout = setTimeout(() => controller.abort(), 20000);
            try {
                const options = {credentials:'include', cache:'no-store', signal:controller.signal};
                if (write) {
                    options.method='POST';
                    options.headers={'Content-Type':'application/json'};
                    options.body=JSON.stringify(params);
                } else { url += '?' + new URLSearchParams(params); }
                const response=await fetch(url, options);
                if (!response.ok) throw Error('HTTP '+response.status);
                return await response.json();
            } finally {clearTimeout(timeout);}
        }""", {"url": API + action, "params": params, "write": point is not None})

    async def read(self):
        return point_ids(await self.request("mark_map_point_list"))

    async def set_mark(self, point, desired):
        current = await self.read()
        if (point in current) == desired:
            return {"changed": False, "confirmed": desired}
        action = "add_mark_map_point" if desired else "new_del_mark_map_point"
        outcome = None
        try:
            reply = await self.request(action, point)
            outcome = {"retcode": reply.get("retcode"), "message": reply.get("message")}
        except Exception as exc:
            # An unanswered POST may have succeeded. Read the same ID before any
            # future retry; never select a replacement marker.
            outcome = {"transport_error": str(exc)}
        for attempt in range(6):
            try:
                if (point in await self.read()) == desired:
                    return {"changed": True, "confirmed": desired, "reply": outcome}
            except Exception:
                pass
            await asyncio.sleep(1 + attempt / 2)
        raise RuntimeError(f"Write could not be confirmed for point {point}: {outcome}")

    async def scene(self):
        return await self.page.evaluate("""() => ({
            hash:location.hash,
            popupCount:document.querySelectorAll('.map-popup').length,
            popupVisible:[...document.querySelectorAll('.map-popup')].some(e=>{
                const r=e.getBoundingClientRect(); return r.width>0 && r.height>0;
            }),
            markButtonCount:[...document.querySelectorAll('button')].filter(e=>
                ['맵 핀','맵 핀 취소'].includes(e.textContent.trim())).length
        })""")


async def run(args):
    existing = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None
    if existing and existing.get("needs_restore") and args.operation != "restore":
        raise RuntimeError("An earlier test needs restoration. Run with --operation restore first.")
    if args.operation == "restore":
        if not existing or not existing.get("needs_restore"):
            raise RuntimeError("No unfinished restoration")
        args.map_id, args.point = existing["map_id"], existing["point_id"]
    report = {"started_utc": datetime.now(timezone.utc).isoformat(),
              "map_id": args.map_id, "point_id": args.point, "operation": args.operation,
              "llm_calls": 0, "steps": [], "success": False}
    save(RESULT, report)
    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            str(ROOT / "official-browser-profile"), channel="chrome", headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-background-timer-throttling"],
        )
        page = browser.pages[0] if browser.pages else await browser.new_page()
        client = OfficialMap(page, args.map_id)
        original = None
        baseline = None
        journal = existing
        try:
            # No label filters, far from the test pin, at the broad map zoom.
            url = MAP + f"#/map/{args.map_id}?shown_types=&center=2008.50,-1084.00&zoom=-3.00"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            print("LOGIN_NEEDED: Log into the official map in the opened Chrome window.", flush=True)
            deadline = asyncio.get_running_loop().time() + args.login_timeout
            last_error = None
            while asyncio.get_running_loop().time() < deadline:
                try:
                    baseline = await client.read()
                    break
                except Exception as exc:
                    last_error = str(exc)
                    await asyncio.sleep(3)
            if baseline is None:
                raise RuntimeError("Login/read unavailable: " + str(last_error))
            print("AUTHENTICATED: mark-list read succeeded", flush=True)
            if args.operation == "restore":
                original = bool(existing["original"])
                report["steps"].append(await client.set_mark(args.point, original))
                journal["needs_restore"] = False
                save(STATE, journal)
            else:
                original = args.point in baseline
                report["baseline"] = {"marked": original, "count": len(baseline), "sha256": digest(baseline)}
                scene = await client.scene()
                report["before_scene"] = scene
                if scene["popupCount"] or scene["markButtonCount"]:
                    raise RuntimeError("Test requires no floating marker popup/buttons")
                journal = {"map_id": args.map_id, "point_id": args.point,
                           "original": original, "needs_restore": True,
                           "baseline_hash": digest(baseline)}
                save(STATE, journal)
                desired = not original
                report["steps"].append({"set_opposite": await client.set_mark(args.point, desired)})
                interim = await client.read()
                if baseline.symmetric_difference(interim) != {args.point}:
                    raise RuntimeError("Unexpected marker changes detected; will restore the test pin only")
                report["steps"].append({"repeat_same_state": await client.set_mark(args.point, desired)})
                await page.reload(wait_until="domcontentloaded")
                after_reload = await client.read()
                if (args.point in after_reload) != desired:
                    raise RuntimeError("Changed state did not survive reload")
                report["changed_reload_confirmed"] = True
                report["after_scene"] = await client.scene()
                await page.screenshot(path=str(ROOT / "inspection" / "machine-mark-no-popup.png"))
        except Exception as exc:
            report["error"] = str(exc)
        finally:
            if original is not None and journal and journal.get("needs_restore"):
                try:
                    report["steps"].append({"restore": await client.set_mark(args.point, original)})
                    await page.reload(wait_until="domcontentloaded")
                    final = await client.read()
                    if (args.point in final) != original:
                        raise RuntimeError("Restoration did not survive reload")
                    report["restored_reload_confirmed"] = True
                    report["final"] = {"count": len(final), "sha256": digest(final),
                                       "same_as_baseline": final == baseline}
                    journal["needs_restore"] = False
                    save(STATE, journal)
                    await page.screenshot(path=str(ROOT / "inspection" / "machine-restored-no-popup.png"))
                except Exception as exc:
                    report["restore_error"] = str(exc)
                    print("RESTORATION_REQUIRED: " + str(exc), flush=True)
            report["success"] = not report.get("error") and not report.get("restore_error")
            if args.operation == "roundtrip":
                report["success"] = bool(report["success"] and report.get("changed_reload_confirmed")
                                         and report.get("restored_reload_confirmed")
                                         and report.get("final", {}).get("same_as_baseline"))
            report["finished_utc"] = datetime.now(timezone.utc).isoformat()
            save(RESULT, report)
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            await browser.close()
    return 0 if report["success"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--map-id", type=int, default=2)
    parser.add_argument("--point", type=int, default=105015)
    parser.add_argument("--operation", choices=["roundtrip", "restore"], default="roundtrip")
    parser.add_argument("--login-timeout", type=int, default=600)
    options = parser.parse_args()
    if options.map_id <= 0 or options.point <= 0:
        parser.error("map and point IDs must be positive")
    raise SystemExit(asyncio.run(run(options)))
