import os
import time
import re
from datetime import datetime, timedelta
from urllib.parse import urljoin
import urllib.parse
import xml.etree.ElementTree as ET
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client, Client
from playwright.sync_api import sync_playwright


def required_secret(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"GitHub Secrets 설정이 필요합니다: {name}")
    return value


url: str = required_secret("SUPABASE_URL")
key: str = required_secret("SUPABASE_KEY")
supabase: Client = create_client(url, key)

G2B_ENDPOINT = "https://apis.data.go.kr/1230000/ad/BidPublicInfoService/getBidPblancListInfoThng"
BIZINFO_ENDPOINT = "https://apis.data.go.kr/1421000/bizinfo/pblancBsnsService"
MSS_BIZ_ENDPOINT = "https://apis.data.go.kr/1421000/mssBizService_v2/getbizList_v2"
SMES_API_ENDPOINT = "https://portal.smes.go.kr/ione-gw/api/pblanc/list"
SERVICE_KEY = required_secret("DATA_GO_KR_SERVICE_KEY")
SMES_API_TOKEN = required_secret("SMES_API_TOKEN")


def safe_error(error):
    message = str(error)
    for secret in (key, SERVICE_KEY, SMES_API_TOKEN):
        for variant in (secret, urllib.parse.quote(secret, safe=""),
                        urllib.parse.quote(secret)):
            if variant:
                message = message.replace(variant, "[인증키 숨김]")
    return message


def run_scraper():
    print("통합 공고 수집기 시작...", flush=True)

    targets = supabase.table('monitoring_sites').select('*').execute().data
    keywords_data = supabase.table('monitoring_keywords').select('keyword').execute().data

    if not keywords_data:
        print("❌ 등록된 키워드가 없습니다.")
        return

    keyword_list = [k['keyword'].replace(" ", "").lower() for k in keywords_data]
    print(f"📌 현재 등록된 검색 키워드: {keyword_list}")

    existing_notices = supabase.table('notices').select('link, title, agency').execute().data
    existing_links = set([n['link'] for n in existing_notices])
    existing_titles = set([
        f"{n['agency']}_{n['title'].replace(' ', '').lower()}"
        for n in existing_notices
    ])

    new_inserts = []
    today = datetime.now()

    # =========================================================================
    # [파트 1] 웹사이트 수집: 사이트별 1~3페이지 모두 확인
    # =========================================================================
    if targets:
        MAX_WEB_PAGES = 3
        print("웹 수집 버전: 3pages-contract-access-v2 (조기 종료 없음)", flush=True)
        # 게시일이 없는 목록은 기존 DB 형식에 맞춰 수집일을 저장한다.
        # 입찰일시/마감일은 게시일로 사용하지 않는다.
        legacy_js = r"""
() => {
const clean = value =>
(value || '').replace(/\s+/g, ' ').trim();
const host = location.hostname;
let selector =
'table tbody tr, table tr, ' +
'ul.board_list > li, ul.notice_list > li, ul.list > li, ' +
'li:has(a[href*="onlineView.do"]), ' +
'li:has(a[href*="w_id="]), ' +
'.card, div[class*="list"], div[class*="item"], ' +
'.board-list > div';
if (host.endsWith('bidpage.co.kr')) {
selector =
'#idNewBidTable > li, ' +
'li:has(a[href*="ViewBid"]), ' +
'tr:has(a[href*="ViewBid"])';
} else if (host === 'sido.jeonnam.go.kr') {
selector = '.toolBoardList tbody tr[onclick]';
} else if (host.endsWith('sbiz24.kr')) {
selector = 'tbody tr.q-tr, tbody tr';
}
const rows = [...document.querySelectorAll(selector)];
const results = [];
const seen = new Set();
const navigationTitles = new Set([
'상세보기', '바로가기', '더보기', '목록',
'처음', '이전', '다음', '마지막',
'로그인', '회원가입', '전체메뉴'
]);
for (const row of rows) {
if (row.querySelector('tr')) continue;
if (
row.tagName === 'DIV' &&
row.querySelector('li, tr, .card')
) continue;
const cells = [
...row.querySelectorAll(':scope > td')
];
let title = '';
let link = '';
let fallback = false;
const fullText = clean(
row.innerText || row.textContent
);
if (!fullText || fullText.length > 3500) continue;
const gosiMatch = (
row.getAttribute('onclick') || ''
).match(
/viewData\(['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]/
);
if (
host === 'sido.jeonnam.go.kr' &&
gosiMatch &&
cells.length >= 4
) {
title = clean(cells[1].textContent);
const detail = new URL(location.href);
detail.searchParams.set('command', 'searchDetail');
detail.searchParams.set('sno', gosiMatch[1]);
detail.searchParams.set('gosiGbn', gosiMatch[2]);
detail.searchParams.delete('currPageNo');
link = detail.href;
} else {
let titleEl = row.querySelector(
'.c_pbancNm, .ntt_sj, td.title, td.subject, ' +
'.title, .subject, .tit, ' +
'td.left, td.al, ' +
'h2, h3, h4, h5, h6, strong, b'
);
let aTag = titleEl
? (
titleEl.matches('a')
? titleEl
: titleEl.querySelector('a')
)
: null;
if (!aTag) {
aTag = row.querySelector(
'a[href*="onlineView.do"], ' +
'a[href*="w_id="], ' +
'a[href*="bbsMode=view"], ' +
'a[href*="articleId="], ' +
'a[href*="boardSeq="], ' +
'a[href*="ViewBid"]'
);
}
if (!aTag) {
const anchors = [
...row.querySelectorAll('a')
].filter(a => {
const text = clean(a.textContent);
return text.length >= 3 &&
!navigationTitles.has(text) &&
!/download|fileDown|attach/i.test(
a.getAttribute('href') || ''
);
});
anchors.sort(
(a, b) =>
clean(b.textContent).length -
clean(a.textContent).length
);
aTag = anchors[0] || null;
}
if (!titleEl) {
titleEl = aTag;
if (!titleEl && cells.length >= 2) {
titleEl =
cells.find(td => td.querySelector('a')) ||
cells[1];
}
}
if (!titleEl) continue;
const anchorTitle = aTag
? clean(aTag.getAttribute('title'))
: '';
title = clean(
anchorTitle &&
!navigationTitles.has(anchorTitle) &&
anchorTitle.length >= clean(
aTag.textContent
).length
? anchorTitle
: (
titleEl.innerText ||
titleEl.textContent
)
).replace(/\s+상세보기$/, '');
if (
title.length < 3 ||
navigationTitles.has(title) ||
title === '공지' ||
title === '안내'
) continue;
const href = aTag
? (aTag.getAttribute('href') || '').trim()
: '';
const action = [
href,
aTag ? aTag.getAttribute('onclick') || '' : '',
titleEl.getAttribute('onclick') || '',
row.getAttribute('onclick') || ''
].join(' ');
const bidMatch = action.match(
/ViewBid\(['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]/
);
if (
host.endsWith('bidpage.co.kr') &&
bidMatch
) {
const detail = new URL(
'/etc/main_popup.htm',
location.href
);
detail.searchParams.set(
'AesCode',
bidMatch[1]
);
detail.searchParams.set(
'BidTypd',
bidMatch[2]
);
link = detail.href;
} else if (
href &&
!/^(javascript:|javasscript:|mailto:|tel:)/i.test(href) &&
(
!href.startsWith('#') ||
href.startsWith('#/')
)
) {
try {
link = new URL(
href,
document.baseURI || location.href
).href;
} catch (_) {}
}
if (!link) {
const urlMatch = action.match(
/['"]((?:https?:\/\/|\/|\.\.?\/|\?)[^'"]+)['"]/
);
if (urlMatch) {
try {
link = new URL(
urlMatch[1],
document.baseURI || location.href
).href;
} catch (_) {}
}
}
if (!link) {
const hasDate =
/\b(?:20\d{2}|\d{2})[-./]\d{1,2}[-./]\d{1,2}\b/
.test(fullText);
const hasAction =
/onclick=|javascript:|cursor:\s*pointer/i
.test(row.outerHTML);
if (!hasDate && !hasAction) continue;
link = location.href;
fallback = true;
}
}
if (!title || !link) continue;
const dateText = clean(
row.querySelector(
'.date, .data, time, .reg_date, .regDate'
)?.textContent
) || fullText;
const dateMatch = dateText.match(
/\b(20\d{2}|\d{2})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})\b/
);
let postDate = '';
if (dateMatch) {
const year = dateMatch[1].length === 2
? '20' + dateMatch[1]
: dateMatch[1];
postDate =
`${year}-` +
`${dateMatch[2].padStart(2, '0')}-` +
`${dateMatch[3].padStart(2, '0')}`;
}
const uniqueKey = fallback
? 'title:' + title.replace(/\s+/g, '')
: link;
if (seen.has(uniqueKey)) continue;
seen.add(uniqueKey);
results.push({
title,
link,
postDate,
fullText,
fallback
});
}
return results;
}
        """
        js_code = r"""
() => {
    const clean = s => (s || '').replace(/\s+/g, ' ').trim();
    const date = s => {
        const m = (s || '').match(/\b(20\d{2}|\d{2})[-./]\s*(\d{1,2})[-./]\s*(\d{1,2})\b/);
        return m ? `${m[1].length === 2 ? '20'+m[1] : m[1]}-${m[2].padStart(2,'0')}-${m[3].padStart(2,'0')}` : '';
    };
    const absolute = href => {
        if (!href || /^(?:javascript:|javasscript:|mailto:|tel:)/i.test(href) ||
            (href.startsWith('#') && !href.startsWith('#/'))) return '';
        try { return new URL(href, document.baseURI || location.href).href; } catch (_) { return ''; }
    };
    const results = [], seen = new Set();
    const add = item => {
        if (!item.title || item.title.length < 3) return;
        const key = item.noticeId || (item.fallback ? item.title : item.link);
        if (!key || seen.has(key)) return;
        seen.add(key); results.push(item);
    };
    // Hanayo: 한 ul이 공고 한 건. 입찰일시는 게시일로 저장하지 않는다.
    const bids = [...document.querySelectorAll('#BIDMainList > ul.ul-body')];
    if (bids.length) {
        for (const row of bids) {
            const a = row.querySelector('li.name a');
            if (!a) continue;
            const source = row.querySelector('li.file a[href*="g2b.go.kr"]');
            const href = a.getAttribute('href') || '';
            const parsed = new URL(href, location.href);
            add({title:clean(a.getAttribute('title') || a.textContent),
                link:absolute(source?.getAttribute('href') || href),
                noticeId:clean(row.querySelector('li.num')?.textContent) || parsed.searchParams.get('no'),
                postDate:'',fullText:clean(row.textContent),fallback:false});
        }
        return results;
    }
    // IRIS: 제목 링크 href가 비어 있는 카드형 공고.
    const iris = [...document.querySelectorAll('ul.dbody > li')];
    if (iris.length && location.hostname.endsWith('iris.go.kr')) {
        for (const row of iris) {
            const a = row.querySelector('strong.title a');
            if (!a) continue;
            const m = (a.getAttribute('onclick') || '').match(/_view\(['"]([^'"]+)['"]/);
            add({title:clean(a.textContent),link:location.href,noticeId:m?.[1] || '',
                postDate:date(row.querySelector('.ancmDe')?.textContent),
                fullText:clean(row.textContent),fallback:true});
        }
        return results;
    }
    const navigation = /^(상세보기|바로가기|더보기|목록|처음|이전|다음|마지막|로그인|회원가입|전체메뉴|공지|안내)$/;
    const dateOnly = /^(?:\d[\d,.\s-]*|첨부파일.*|조회수.*)$/;
    const preferredTables = [...document.querySelectorAll(
        'table.board_list,table.boardList,table.bbs_table,table.bbsList,table.table_board,table.toolBoardList')];
    let rows = preferredTables.length
        ? preferredTables.flatMap(t=>[...t.querySelectorAll('tbody > tr')])
        : [...document.querySelectorAll('table tbody > tr')];
    rows = rows.concat([...document.querySelectorAll(
        '#idNewBidTable > li,li:has(a[href*="onlineView.do"]),li:has(a[href*="w_id="]),ul.board_list > li,ul.notice_list > li,ul.list > li,.card,.board-list > div,tr.q-tr,ul:has(> li.title a[onclick*="searchDetail"])')]);
    for (const row of rows) {
        if (row.querySelector('tr')) continue;
        const text = clean(row.innerText || row.textContent);
        if (!text || text.length > 2200) continue;
        const tds = [...row.querySelectorAll(':scope > td')];
        const titleCell = row.querySelector('.c_pbancNm,td.title,td.subject,td.tit,td.title_minwon,td.title_wrap,.ntt_sj,.subject,.tit,.title,h3,h4');
        let a = titleCell?.querySelector('a') || (titleCell?.matches('a') ? titleCell : null);
        if (!a) a = [...row.querySelectorAll('a')].filter(e=> {
            const s=clean(e.textContent);return s.length>3 && !navigation.test(s) && !dateOnly.test(s)
                && !/download|fileDown|attach/i.test(e.getAttribute('href')||'');
        }).sort((a,b)=>clean(b.textContent).length-clean(a.textContent).length)[0];
        let title = clean(a?.getAttribute('title') || a?.textContent || titleCell?.textContent);
        if (!title && tds.length>=4 && (row.matches('[onclick*="searchDetail"], [onclick*="viewData"]') || row.querySelector('[onclick*="searchDetail"], [onclick*="viewData"]'))) title=clean(tds[1].textContent);
        title=title.replace(/\s+(?:에 대한 글내용 보기\.?|상세보기)$/,'');
        if (title.length<3 || navigation.test(title) || dateOnly.test(title)) continue;
        const action = [a?.getAttribute('href'),a?.getAttribute('onclick'),row.getAttribute('onclick'),titleCell?.getAttribute('onclick')].join(' ');
        let link=absolute(a?.getAttribute('href')),fallback=false,id='';
        const gosi=action.match(/viewData\(['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]/);
        const bid=action.match(/ViewBid\(['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]/);
        const detail=action.match(/searchDetail\(['"]([^'"]+)['"]/);
        if(gosi && /SAPGosiBizProcess/.test(location.href)) {
            if (tds[1]) title=clean(tds[1].textContent);
            const u=new URL(location.href);u.searchParams.set('command','searchDetail');
            u.searchParams.set('sno',gosi[1]);u.searchParams.set('gosiGbn',gosi[2]);
            link=u.href;id=gosi[1];
        } else if(bid && location.hostname.endsWith('bidpage.co.kr')) {
            const u=new URL('/etc/main_popup.htm',location.href);
            u.searchParams.set('AesCode',bid[1]);u.searchParams.set('BidTypd',bid[2]);link=u.href;id=bid[1];
        } else if(detail) {
            id=detail[1];
            // 곡성: 실제 inline 함수의 /board/GosiView.do 동작을 확인한 경로.
            if(location.hostname.endsWith('gokseong.go.kr')) {
                const u=new URL('/board/GosiView.do',location.href);
                u.searchParams.set('not_ancmt_mgt_no',id);
                const menu=new URL(location.href).searchParams.get('menuNo');
                if(menu)u.searchParams.set('menuNo',menu);link=u.href;
            }
        }
        if(!link){link=location.href;fallback=true;}
        // 날짜/상세 ID 없이 일반 화면 요소만 있는 표는 공고에서 제외.
        if(!id && !date(text) && !/[?&](?:postUid|nttId|boardSeq|articleId|key|seq|w_id|onlineSn)=|#\//i.test(link) && !titleCell)continue;
        let dateText=clean(row.querySelector('.ancmDe,.reg_date,.regDate,.created,td.date,li.col04,time')?.textContent);
        if(!dateText && !row.closest('table.table_board') && !/입찰일시|개찰일시|마감일/.test(text))dateText=text;
        add({title,link,noticeId:id,postDate:date(dateText),fullText:text,fallback});
    }
    return results;
}

        """
        pager_js = r"""
(number) => {
    const controls=[...document.querySelectorAll('a,button,input[type="button"]')];
    const candidates=[];
    for(let index=0;index<controls.length;index++){
        const e=controls[index];
        const text=(e.textContent||e.value||'').replace(/\s+/g,' ').trim();
        const alt=e.querySelector('img')?.getAttribute('alt')||'';
        const href=(e.getAttribute('href')||'').trim();
        const action=e.getAttribute('onclick')||'';
        if(e.disabled||e.getAttribute('aria-disabled')==='true'||/disabled/.test(e.className))continue;
        if(/searchDetail|viewData|ViewBid|_view\(/i.test(action+' '+href))continue;
        const pager=e.closest('#PageList,.paginate,.pagination,.paging,.paging_area,.page_num,.pagenate,.pagenation,.paging-wrap,.page_list,.page_wrap,.pagination_wrap,.board_paging,.paging_area2');
        const pageSignal=/[?&](?:page|pageIndex|pageNo|currentPage|currPageNo)=|goPage|page_link|linkPage|pageIndex\.value|siiruPg-page/i.test(href+' '+action+' '+e.getAttribute('data-tp'));
        const numeric=text===String(number);
        const moved=e.getAttribute('data-move');
        const next=/^(다음|다음 페이지|다음페이지|next|>|〉)$/i.test(text||alt);
        if(!(numeric&&(pager||pageSignal||e.tagName==='BUTTON'))&&!(moved===String(number))&&!(next&&pager))continue;
        let url='';
        if(href&&!action&&!href.startsWith('#')&&!/^javascript:/i.test(href)){
            try{
                const u=new URL(href,document.baseURI||location.href);
                const p=[...u.searchParams].find(([k])=>/^(page|pageIndex|pageNo|currPageNo|currentPage)$/i.test(k));
                if(p&&Number(p[1])!==number)continue;
                if(u.origin!==location.origin)continue;
                url=u.href;
            }catch(_){continue;}
        }
        if(!url&&!action&&e.tagName!=='BUTTON'&&!moved)continue;
        // IRIS에서 관측된 '(2); return false;'는 실제 페이지 이동 함수가 아니다.
        if(!url&&/^\s*\(\d+\);?\s*return false;?\s*$/.test(action)){
            candidates.push({index,url:'',invalid:true});continue;
        }
        candidates.push({index,url,rank:numeric||moved?0:1});
    }
    // 숫자 페이지 영역이 있고 요청 페이지가 없으면 정상적인 마지막 페이지.
    return candidates.sort((a,b)=>(a.rank??9)-(b.rank??9));
}

        """

        def normalized_link(value):
            u = urllib.parse.urlsplit(value or '')
            if u.scheme not in ('http', 'https'):
                return ''
            ignored = {'page', 'pageindex', 'pageno', 'cpage', 'currpageno',
                       'per_page', 'renderid', 'cline', 'searchkeyword',
                       'searchcondition', 'searchstartdt', 'searchenddt',
                       'keywordidx', '_gl', '_ga'}
            query = sorted((k, v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                           if k.lower() not in ignored and v != '')
            return urllib.parse.urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path or '/',
                urllib.parse.urlencode(query), u.fragment if u.fragment.startswith('/') else ''))

        # 기존 공고를 페이지 단위로 전부 확인. 등록 사이트 자체는 수정하지 않는다.
        offset = 0
        while True:
            batch = supabase.table('notices').select('link,title,agency').order('id').range(offset, offset + 499).execute().data or []
            for notice in batch:
                if notice.get('link'):
                    existing_links.add(notice['link'])
                existing_titles.add(f"{notice.get('agency', '')}_{(notice.get('title') or '').replace(' ', '').lower()}")
            if len(batch) < 500:
                break
            offset += len(batch)
        known_links = {normalized_link(link) for link in existing_links if link}

        def add_items(name, items, base):
            added = 0
            fallback_count = 0
            for item in items:
                title = re.sub(r'\s+', ' ', item.get('title') or '').strip()
                if len(title) < 3:
                    continue
                clean_title = title.replace(' ', '').lower()
                full = (item.get('fullText') or '').replace(' ', '').lower()
                keyword = next((kw for kw in keyword_list if kw and kw in clean_title), None)
                keyword = keyword or next((kw for kw in keyword_list if kw and kw in full), None)
                if not keyword:
                    continue
                fallback = bool(item.get('fallback'))
                link = urljoin(base, item.get('link') or base)
                normalized = normalized_link(link)
                title_key = f'{name}_{clean_title}'
                if not normalized or title_key in existing_titles or (not fallback and normalized in known_links):
                    continue
                post_date = item.get('postDate') or today.strftime('%Y-%m-%d')
                try:
                    datetime.strptime(post_date, '%Y-%m-%d')
                except ValueError:
                    post_date = today.strftime('%Y-%m-%d')
                existing_titles.add(title_key)
                if not fallback:
                    existing_links.add(link)
                    known_links.add(normalized)
                else:
                    fallback_count += 1
                new_inserts.append({'agency': name, 'title': title, 'link': link,
                                    'post_date': post_date, 'matched_keyword': keyword})
                added += 1
            return added, fallback_count

        def effective_url(value):
            u = urllib.parse.urlsplit(value)
            # 공식 홈페이지에서 확인한 연결만 사용. Supabase 등록값은 변경하지 않는다.
            fixes = {
                'eminwon.jeongeup.go.kr': 'https://gyeyak.jeongeup.go.kr/bid',
                'eminwon.hwasun.go.kr': 'http://gyeyak.hwasun.go.kr/menu_02.html',
                'eminwon.muju.go.kr': 'http://copen.muju.go.kr/bid',
            }
            if u.hostname in fixes and u.path.endswith('/OfrAction.do') and not u.query:
                return fixes[u.hostname]
            if u.hostname == 'gyeyak.wando.go.kr' and u.path in ('', '/', '/index'):
                return 'https://gyeyak.wando.go.kr/bid'
            if u.hostname == 'www.namwon.go.kr' and u.path.endswith('/list.do'):
                q = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
                q = [(k, '1' if k.lower() == 'page' else v) for k, v in q]
                return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))
            return value

        def get_items(frame):
            code = legacy_js if 'kosha.or.kr' in urllib.parse.urlsplit(frame.url).netloc else js_code
            return frame.evaluate(code)

        def fingerprint(items):
            return tuple(sorted({(item.get('noticeId') or normalized_link(item.get('link')) or '',
                                  item.get('title', '')) for item in items}))

        def wait_items(page, previous=None, seconds=22):
            deadline = time.monotonic() + seconds
            last_errors = []
            while time.monotonic() < deadline:
                try:
                    frames = [f for f in page.frames if not f.is_detached()]
                    # 공고 iframe이 있으면 바깥 페이지의 메뉴/표는 추출하지 않는다.
                    preferred = [f for f in frames if re.search(r'hanayo\.net|/emwp/|SAPGosiBizProcess', f.url, re.I)]
                    for frame in preferred or frames:
                        try:
                            items = get_items(frame)
                            if items and (previous is None or fingerprint(items) != previous):
                                return frame, items
                        except Exception as exc:
                            last_errors = [type(exc).__name__]
                except Exception:
                    pass
                page.wait_for_timeout(400)
            return None, []

        def goto_list(page, link):
            try:
                response = page.goto(link, wait_until='domcontentloaded', timeout=25000)
                if response and response.status >= 400:
                    raise RuntimeError(f'HTTP {response.status}')
            except Exception:
                # DOMContentLoaded 지연이어도 실제 공고가 이미 있으면 사용한다.
                for frame in page.frames:
                    try:
                        if get_items(frame):
                            return
                    except Exception:
                        pass
                raise

        def move_page(frame, number):
            choices = frame.evaluate(pager_js, number)
            for choice in choices:
                if choice.get('invalid'):
                    raise RuntimeError('사이트 페이지 버튼의 JavaScript에 이동 함수가 없음')
                try:
                    if choice.get('url'):
                        # 실제 페이지 href를 직접 열어 클릭 직후 컨텍스트 오류를 줄인다.
                        frame.goto(choice['url'], wait_until='domcontentloaded', timeout=20000)
                    else:
                        control = frame.locator('a,button,input[type="button"]').nth(choice['index'])
                        if not control.is_visible() or not control.is_enabled():
                            continue
                        control.click(timeout=7000, no_wait_after=True)
                    return 'moved'
                except Exception as exc:
                    if re.search(r'context.*destroyed|navigation|Timeout', str(exc), re.I):
                        return 'check'
                    continue
            return 'end'

        def prepare_listing(page):
            # 계약정보 메인 화면은 화면에 실제 제공되는 입찰 목록 링크를 따라간다.
            if not any('hanayo.net/main2024' in f.url for f in page.frames):
                link = page.evaluate(r'''() => {
                    if (!/계약정보/.test(document.title)) return '';
                    const a = [...document.querySelectorAll('a[href]')].find(a =>
                        a.textContent.trim() === '입찰공고' && /\/bid(?:$|\?)/.test(a.href));
                    return a ? a.href : '';
                }''')
                if link and normalized_link(link) != normalized_link(page.url):
                    goto_list(page, link)
            # 실제 공개 iframe src로 직접 이동한다. 등록 주소는 변경하지 않는다.
            embedded = page.evaluate(r"""() => [...document.querySelectorAll('iframe[src],frame[src]')]
                .map(e => e.src).filter(src => /^https?:/.test(src) &&
                    /\/emwp\/|SAPGosiBizProcess/.test(src))""")
            for src in embedded:
                if src != page.url:
                    goto_list(page, src)
                    break
            page.wait_for_timeout(800)

        # 공개된 외부 입찰 게시판을 직접 읽는다. iframe 자동 로딩에 의존하지 않는다.
        # 아래 gcode는 실제 지자체 iframe src에서 확인한 값이다.
        HANAYO_SITES = {
            'gyeyak.mokpo.go.kr': 'mokpo',
            'gyeyak.gunsan.go.kr': 'gunsan',
            'gyeyak.jangsu.go.kr': 'jangsu',
            'gyeyak.iksan.go.kr': 'iksan',
            'gyeyak.gimje.go.kr': 'gimje',
            'gyeyak.jinan.go.kr': 'jinan',
            'gyeyak.imsil.go.kr': 'imsil',
            'gyeyak.gochang.go.kr': 'gochang',
            'www.sunchang.go.kr': 'sunchang',
            'gyeyak.yeongam.go.kr': 'yeongam',
            'gyeyak.hampyeong.go.kr': 'hampyeong',
            'gyeyak.gwangyang.go.kr': 'gwangyang',
            'geyak.yeonggwang.go.kr': 'yeonggwang',
            'jangseong.go.kr': 'jangseong',
            'gyeyak.wando.go.kr': 'wando',
            'gyeyak.gangjin.go.kr': 'gangjin',
            'gyeyak.jeongeup.go.kr': 'jeongeup',
            'gyeyak.hwasun.go.kr': 'hwasun',
            'copen.muju.go.kr': 'muju',
            'gyeyak.gurye.go.kr': 'gurye',
            'gyeyak.muan.go.kr': 'muan',
        }

        def is_contract_target(link):
            u = urllib.parse.urlsplit(link)
            host = u.hostname or ''
            if host in {'www.sunchang.go.kr', 'jangseong.go.kr'}:
                return '/gyeyak/' in u.path
            return host in HANAYO_SITES

        def hanayo_url(value):
            u = urllib.parse.urlsplit(value)
            if u.hostname != 'g.hanayo.net' or u.path != '/main2024.html':
                raise ValueError('확인된 공개 입찰 게시판 주소가 아닙니다.')
            q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
            code = q.get('gcode', '').strip()
            if not code:
                raise ValueError('입찰 게시판의 기관 코드(gcode)가 없습니다.')
            # 기관 코드만 공백을 정리하고 원래 필터를 보존한다.
            q['gcode'] = code
            q['type'] = 'bid'
            q.setdefault('btype', '')
            q['page'] = '1'
            return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q), fragment=''))

        from html.parser import HTMLParser

        class PublicBidHTML(HTMLParser):
            def __init__(self, base):
                super().__init__(convert_charrefs=True)
                self.base = base
                self.rows = []
                self.anchors = []
                self.frames = []
                self.row = None
                self.row_depth = 0
                self.field = None
                self.anchor = None
                self.has_list = False

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                classes = (attrs.get('class') or '').split()
                if attrs.get('id') == 'BIDMainList':
                    self.has_list = True
                if tag in ('iframe', 'frame') and attrs.get('src'):
                    self.frames.append(urljoin(self.base, attrs['src']))
                if tag == 'ul':
                    if self.row is not None:
                        self.row_depth += 1
                    elif 'ul-body' in classes:
                        self.row = {'fields': {}, 'text': [], 'anchors': []}
                        self.row_depth = 1
                if tag == 'li' and self.row is not None:
                    self.field = next((c for c in classes if c in {
                        'name', 'num', 'category2', 'dept', 'time', 'file', 'method', 'price'
                    }), None)
                if tag == 'a':
                    self.anchor = {
                        'href': urljoin(self.base, attrs.get('href', '')),
                        'title': attrs.get('title', ''), 'text': [], 'field': self.field,
                    }

            def handle_data(self, text):
                if self.row is not None:
                    self.row['text'].append(text)
                    if self.field:
                        self.row['fields'].setdefault(self.field, []).append(text)
                if self.anchor is not None:
                    self.anchor['text'].append(text)

            def handle_endtag(self, tag):
                if tag == 'a' and self.anchor is not None:
                    self.anchor['text'] = ' '.join(''.join(self.anchor['text']).split())
                    self.anchors.append(self.anchor)
                    if self.row is not None:
                        self.row['anchors'].append(self.anchor)
                    self.anchor = None
                if tag == 'li':
                    self.field = None
                if tag == 'ul' and self.row is not None:
                    self.row_depth -= 1
                    if self.row_depth == 0:
                        self.rows.append(self.row)
                        self.row = None
                        self.field = None

            def items(self):
                items, seen = [], set()
                for row in self.rows:
                    a = next((a for a in row['anchors'] if a['field'] == 'name'), None)
                    if not a:
                        continue
                    title = re.sub(r'\s+', ' ', a['title'] or a['text']).strip()
                    if not title or title in {'입찰건명', '제목'}:
                        continue
                    official = next((a for a in row['anchors'] if
                        urllib.parse.urlsplit(a['href']).hostname in {'www.g2b.go.kr', 'g2b.go.kr'}
                        and 'bidPbancNo=' in a['href']), None)
                    code = ''.join(row['fields'].get('num', [])).strip()
                    identity = code or dict(urllib.parse.parse_qsl(
                        urllib.parse.urlsplit(a['href']).query)).get('no')
                    if not identity or identity in seen:
                        continue
                    seen.add(identity)
                    items.append({
                        'title': title, 'link': official['href'] if official else a['href'],
                        'noticeId': identity, 'postDate': '',
                        'fullText': ' '.join(' '.join(row['text']).split()),
                        'fallback': False,
                    })
                return items

            def next_page(self, number):
                current = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.base).query))
                for a in self.anchors:
                    u = urllib.parse.urlsplit(a['href'])
                    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
                    if (a['text'] == str(number) and u.hostname == 'g.hanayo.net'
                            and u.path == '/main2024.html' and not q.get('no')
                            and q.get('type') == 'bid' and q.get('page') == str(number)
                            and q.get('gcode', '').strip() == current.get('gcode', '').strip()):
                        return a['href']
                return None

        def contract_source(link):
            host = urllib.parse.urlsplit(link).hostname or ''
            code = HANAYO_SITES.get(host)
            if code:
                # 지자체 화면에 공개된 동일 공급자의 목록. 부모 페이지의 403과 분리해 조회.
                return hanayo_url('https://g.hanayo.net/main2024.html?' +
                    urllib.parse.urlencode({'gcode': code, 'type': 'bid', 'btype': ''}))
            final, html = read_public_html(link, link)
            parser = PublicBidHTML(final)
            parser.feed(html)
            parser.close()
            for src in parser.frames:
                u = urllib.parse.urlsplit(src)
                if u.hostname == 'g.hanayo.net' and u.path == '/main2024.html':
                    return hanayo_url(src)
            # 메인 화면이라면 실제 표시된 '입찰공고' 링크만 따라간다.
            for a in parser.anchors:
                u = urllib.parse.urlsplit(a['href'])
                if a['text'] == '입찰공고' and u.hostname == host and u.path.endswith('/bid') and a['href'] != final:
                    final2, html2 = read_public_html(a['href'], final)
                    inner = PublicBidHTML(final2)
                    inner.feed(html2)
                    inner.close()
                    for src in inner.frames:
                        u = urllib.parse.urlsplit(src)
                        if u.hostname == 'g.hanayo.net' and u.path == '/main2024.html':
                            return hanayo_url(src)
            raise RuntimeError('외부 입찰 iframe 주소 미확인: 기관 코드 추측 없이 중단')

        import json
        import gzip
        import io
        import http.cookiejar
        from urllib import request as public_request, error as public_error
        from pathlib import Path

        access_dir = Path(os.environ.get('NOTICE_DIAGNOSTICS_DIR', '.notice-diagnostics'))
        access_dir.mkdir(parents=True, exist_ok=True)
        public_opener = public_request.build_opener(
            public_request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        public_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        access_number = [0]

        def access_record(link, route, status=None, html='', error='', rows=None):
            access_number[0] += 1
            u = urllib.parse.urlsplit(link)
            q = dict(urllib.parse.parse_qsl(u.query))
            category = ('ok' if rows is not None else
                        'response_received' if status is not None and status < 400 and not error else 'request_failed')
            if status in (401, 403, 429):
                category = 'access_rejected'  # IP 차단이라는 뜻으로 단정하지 않는다.
            elif 'CERTIFICATE_VERIFY_FAILED' in error:
                category = 'certificate_error'
            elif re.search(r'Name or service not known|NAME_NOT_RESOLVED|getaddrinfo|nodename', error, re.I):
                category = 'dns_error'
            elif re.search(r'timed? ?out|Timeout', error, re.I):
                category = 'timeout'
            elif html and rows is None and error:
                category = 'unexpected_html'
            if rows is None and 'BIDMainList' not in html and re.search(r'비인가|접근.{0,10}차단|access denied|captcha|verify you are human', html, re.I):
                category = 'verification_or_block_page'
            record = {'route': route, 'url': link, 'status': status, 'category': category,
                      'rows': rows, 'error': error[:1200], 'html_characters': len(html)}
            # 응답 헤더의 Cookie/Authorization 및 환경변수는 기록하지 않는다.
            if rows is None and html:
                record['response_excerpt'] = re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html))[:1800]
            identity = re.sub(r'[^a-zA-Z0-9_.-]', '_', q.get('gcode', 'public'))
            page_label = re.sub(r'[^0-9]', '', q.get('page', '1')) or '1'
            filename = f"{access_number[0]:04d}-{identity}-p{page_label}-{route}.json"
            (access_dir / filename).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f'↳ 입찰 접속 [{route}] HTTP {status if status is not None else "-"} / {category}' +
                  (f' / 공고 {rows}건' if rows is not None else f' / {error[:160]}'), flush=True)

        def decode_public_body(body, charset=None):
            found = re.search(br'charset\s*=\s*["\']?\s*([\w-]+)', body[:8000], re.I)
            encoding = found[1].decode('ascii') if found else charset or 'utf-8'
            if encoding.lower().replace('-', '') in {'euckr', 'ksc56011987'}:
                encoding = 'cp949'
            try:
                return body.decode(encoding, errors='replace')
            except LookupError:
                return body.decode('utf-8', errors='replace')

        def read_public_html(link, referer):
            headers = {'User-Agent': public_user_agent, 'Accept': 'text/html,application/xhtml+xml',
                       'Accept-Language': 'ko-KR,ko;q=0.9', 'Accept-Encoding': 'identity',
                       'Referer': referer}
            # 일시적 429/503에 한해서 한 번만 재시도. 403에는 반복 요청하지 않는다.
            for attempt in range(2):
                try:
                    req = public_request.Request(link, headers=headers)
                    with public_opener.open(req, timeout=18) as response:
                        body = response.read(4 * 1024 * 1024 + 1)
                        if len(body) > 4 * 1024 * 1024:
                            raise RuntimeError('목록 응답 크기 제한 초과')
                        if response.headers.get('Content-Encoding', '').lower() == 'gzip':
                            body = gzip.GzipFile(fileobj=io.BytesIO(body)).read(4 * 1024 * 1024 + 1)
                            if len(body) > 4 * 1024 * 1024:
                                raise RuntimeError('압축 해제 응답 크기 제한 초과')
                        html = decode_public_body(body, response.headers.get_content_charset())
                        access_record(response.geturl(), 'http', response.status, html)
                        return response.geturl(), html
                except public_error.HTTPError as exc:
                    body = exc.read(32768)
                    html = decode_public_body(body, exc.headers.get_content_charset())
                    access_record(link, 'http', exc.code, html, str(exc))
                    retry_after = exc.headers.get('Retry-After', '')
                    if attempt == 0 and exc.code in (429, 503) and retry_after.isdigit() and int(retry_after) <= 3:
                        time.sleep(max(1, int(retry_after)))
                        continue
                    raise RuntimeError(f'공개 목록 HTTP {exc.code}; .notice-diagnostics 응답 기록 확인') from exc
                except Exception as exc:
                    access_record(link, 'http', error=f'{type(exc).__name__}: {exc}')
                    raise

        def parse_public_page(link, html):
            parser = PublicBidHTML(link)
            parser.feed(html)
            parser.close()
            if not parser.has_list:
                raise RuntimeError('공고 목록 BIDMainList 없음: 차단 응답 또는 다른 HTML')
            items = parser.items()
            if not items and not re.search(r'전체\s*(?:<[^>]+>|\s)*0\s*(?:<[^>]+>|\s)*건|등록된.{0,15}없|검색된.{0,15}없', html):
                raise RuntimeError('목록은 있으나 공고 추출 0건: 구조/로딩 확인 필요')
            return items, parser

        def fetch_contract_page(link, referer, context):
            errors = []
            try:
                final, html = read_public_html(link, referer)
                items, parser = parse_public_page(final, html)
                access_record(final, 'http-parse', 200, html, rows=len(items))
                return final, items, parser
            except Exception as exc:
                errors.append(f'HTTP: {str(exc)[:220]}')
            # 브라우저의 정상 쿠키 저장소를 공유하는 요청 경로.
            try:
                response = context.request.get(link, headers={'Referer': referer}, timeout=15000)
                try:
                    body = response.body()
                    if len(body) > 4 * 1024 * 1024:
                        raise RuntimeError('브라우저 HTTP 응답 크기 제한 초과')
                    html = decode_public_body(body)
                    status, final = response.status, response.url
                    if status >= 400:
                        access_record(final, 'browser-http', status, html, '공개 목록 요청 거절')
                        raise RuntimeError(f'HTTP {status}')
                    items, parser = parse_public_page(final, html)
                    access_record(final, 'browser-http', status, html, rows=len(items))
                    return final, items, parser
                finally:
                    response.dispose()
            except Exception as exc:
                errors.append(f'브라우저 HTTP: {str(exc)[:220]}')
            probe = context.new_page()
            status, html = None, ''
            try:
                response = probe.goto(link, referer=referer, wait_until='domcontentloaded', timeout=25000)
                status = response.status if response else None
                if status is not None and status >= 400:
                    html = probe.content()
                    raise RuntimeError(f'HTTP {status}')
                probe.locator('#BIDMainList').wait_for(state='attached', timeout=8000)
                html = probe.content()
                items, parser = parse_public_page(probe.url, html)
                access_record(probe.url, 'browser-page', status, html, rows=len(items))
                return probe.url, items, parser
            except Exception as exc:
                if not html:
                    try:
                        html = probe.content()
                    except Exception:
                        pass
                access_record(link, 'browser-page', status, html, str(exc))
                errors.append(f'브라우저 페이지: {str(exc)[:220]}')
                raise RuntimeError(' / '.join(errors) + ' / GitHub에서만 실패하면 PC 실행 결과와 비교 필요') from exc
            finally:
                probe.close()

        def export_contract_items(name, items, source, number):
            # 키워드 필터 전 공고를 파일로 남겨 추출 성공 여부를 직접 확인한다.
            with (access_dir / 'contract_notices.jsonl').open('a', encoding='utf-8') as output:
                for item in items:
                    row = {'agency': name, 'page': number, 'source': source,
                           'notice_id': item.get('noticeId'), 'title': item['title'], 'link': item['link']}
                    output.write(json.dumps(row, ensure_ascii=False) + '\n')

        def collect_contract(name, link, context):
            source = contract_source(link)
            print(f'↳ 공개 iframe 목록 직접 수집: {source}', flush=True)
            visited = set()
            for number in range(1, MAX_WEB_PAGES + 1):
                final, items, parser = fetch_contract_page(source, link, context)
                if not items:
                    if number == 1:
                        print(f'↳ {name}: 입찰 목록 응답 확인, 게시물 0건', flush=True)
                        return
                    raise RuntimeError('페이지 이동 후 공고 0건: 이전 목록과 혼동하지 않고 중단')
                sig = tuple(sorted(item['noticeId'] for item in items))
                if sig in visited:
                    raise RuntimeError('입찰 공고번호 목록 반복: 페이지 이동 실패')
                visited.add(sig)
                export_contract_items(name, items, final, number)
                added, fallback_count = add_items(name, items, final)
                print(f'📄 {name} {number}/{MAX_WEB_PAGES}페이지: 입찰공고 {len(items)}건, 신규 {added}건', flush=True)
                if number == MAX_WEB_PAGES:
                    return
                next_url = parser.next_page(number + 1)
                if not next_url:
                    print(f'↳ {name}: 마지막 페이지, 정상 종료', flush=True)
                    return
                source = next_url
                time.sleep(0.3)
        failures = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=os.environ.get('NOTICE_HEADLESS', '1') != '0')
            context = browser.new_context(
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                viewport={'width': 1920, 'height': 1080}, locale='ko-KR')
            context.set_default_timeout(8000)
            for target in targets:
                name = target.get('site_name') or ''
                raw = target.get('site_url') or ''
                site_filter = os.environ.get('NOTICE_SITE_FILTER', '').strip().lower()
                if not raw or 'api연동' in raw or name in {'중소벤처기업부', '중소벤처기업부_사업', '중소벤처기업부_사업공고', '중소벤처24'}:
                    continue
                # URL의 정상적인 콤마 값(01,04 등)은 자르지 않는다.
                links = [s.strip() for s in re.split(r'\s*,\s*(?=https?://)', raw) if s.strip().startswith(('http://', 'https://'))]
                for original in links:
                    if site_filter and site_filter not in (name + ' ' + original + ' ' + effective_url(original)).lower():
                        continue
                    if name == '나라장터' and 'bidpage.co.kr' not in original:
                        continue
                    page = context.new_page()
                    page.on('dialog', lambda dialog: dialog.dismiss())
                    stage = '접속'
                    try:
                        link = effective_url(original)
                        print(f'🔍 {name} ({link}): 1~3페이지 모두 확인', flush=True)
                        if link != original:
                            print('↳ 확인된 공식 목록 경로 사용 (등록 주소는 유지)', flush=True)
                        if is_contract_target(link):
                            stage = '외부 입찰 목록 수집'
                            collect_contract(name, link, context)
                            continue
                        goto_list(page, link)
                        if 'kosha.or.kr' in link or 'sbiz24.kr' in link:
                            try:
                                page.wait_for_load_state('networkidle', timeout=12000)
                            except Exception:
                                pass
                            page.wait_for_timeout(3000)
                        prepare_listing(page)
                        stage = '목록 로딩'
                        frame, items = wait_items(page, seconds=30)
                        visited = set()
                        for number in range(1, MAX_WEB_PAGES + 1):
                            if not items:
                                raise RuntimeError('공고 목록 미검출: iframe 연결/조회 파라미터/사이트 응답 확인 필요')
                            sig = fingerprint(items)
                            if sig in visited:
                                raise RuntimeError('같은 공고 목록 반복: 페이지 이동 중단')
                            visited.add(sig)
                            stage = f'{number}페이지 수집'
                            added, fallback_count = add_items(name, items, frame.url)
                            print(f'📄 {name} {number}/{MAX_WEB_PAGES}페이지: 공고 {len(items)}건, 신규 {added}건', flush=True)
                            if fallback_count:
                                print(f'↳ 상세주소 미확인 {fallback_count}건: 목록 링크로 저장', flush=True)
                            if number == MAX_WEB_PAGES:
                                break
                            stage = f'{number + 1}페이지 이동'
                            state = move_page(frame, number + 1)
                            if state == 'end':
                                print(f'↳ {name}: 다음 페이지 없음, 정상 종료', flush=True)
                                break
                            next_frame, next_items = wait_items(page, previous=sig, seconds=22)
                            if not next_items:
                                raise RuntimeError('페이지 이동 후 공고 ID/제목이 바뀌지 않음')
                            frame, items = next_frame, next_items
                    except Exception as exc:
                        failures.append(name)
                        message = str(exc).splitlines()[0][:220] or type(exc).__name__
                        print(f'⚠️ {name} / {stage}: {message}', flush=True)
                    finally:
                        page.close()
            context.close()
            browser.close()
        if failures:
            print('⚠️ 웹 수집 일부 실패: ' + ', '.join(dict.fromkeys(failures)), flush=True)
        if os.environ.get('NOTICE_DIAGNOSTIC_ONLY') == '1':
            print('입찰 진단 실행 종료: .notice-diagnostics/contract_notices.jsonl 확인 (DB 저장/API 실행 없음)', flush=True)
            return

    # =========================================================================
    # 공통 API 통신 세션 설정
    # =========================================================================
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=2, status_forcelist=[500, 502, 503, 504])
    session.mount('https://', HTTPAdapter(max_retries=retries))

    # =========================================================================
    # [파트 2] 나라장터 API 수집
    # =========================================================================
    try:
        print("🌐 나라장터 API 요청 중...")
        params = {
            'serviceKey': SERVICE_KEY, 'inqryDiv': '1',
            'inqryBgnDt': (today - timedelta(days=7)).strftime('%Y%m%d0000'),
            'inqryEndDt': today.strftime('%Y%m%d2359'),
            'numOfRows': '100', 'pageNo': '1', 'type': 'json'
        }
        res = session.get(G2B_ENDPOINT, params=params, timeout=30).json()
        items = res.get('response', {}).get('body', {}).get('items', [])

        print(f"📦 나라장터에서 받아온 공고 수: {len(items)}건")

        matched_count = 0
        for item in items:
            title = item.get('bidNtceNm', '').strip()
            link = item.get('bidNtceDtlUrl', 'https://www.g2b.go.kr')
            notice_date = item.get('bidNtceDt', today.strftime('%Y-%m-%d'))[:10]
            if not title: continue

            clean_title = title.replace(" ", "").lower()
            matched_keyword = next((kw for kw in keyword_list if kw in clean_title), None)
            title_key = f"나라장터_{clean_title}"

            if matched_keyword:
                matched_count += 1
                if link not in existing_links and title_key not in existing_titles:
                    existing_links.add(link)
                    existing_titles.add(title_key)
                    new_inserts.append({'agency': "나라장터", 'title': title, 'link': link, 'post_date': notice_date, 'matched_keyword': matched_keyword})

        print(f"🎯 나라장터 키워드 매칭 건수: {matched_count}건")
    except Exception as e: print(f"❌ 나라장터 API 예외 발생: {safe_error(e)}")

    # =========================================================================
    # [파트 3] 중소벤처기업부 API 수집
    # =========================================================================
    try:
        print("🌐 중소벤처기업부 API 요청 중...")
        biz_params = {
            'serviceKey': SERVICE_KEY,
            'numOfRows': '100',
            'pageNo': '1'
        }
        res = session.get(BIZINFO_ENDPOINT, params=biz_params, timeout=30)

        items = []
        res_text = res.text.strip()

        if res_text.startswith('<'):
            try:
                for el in ET.fromstring(res_text).iter('item'):
                    items.append({child.tag: (child.text or '') for child in el})
            except Exception as xml_e: pass
        elif res_text.startswith('{') or res_text.startswith('['):
            try:
                data = res.json()
                items = data.get('response', {}).get('body', {}).get('items', [])
                if not items and isinstance(data.get('items'), list):
                    items = data.get('items')
            except Exception as json_e: pass

        print(f"📦 중소벤처기업부에서 받아온 공고 수: {len(items)}건")

        matched_count = 0
        if items:
            for item in items:
                title = (item.get('pblancNm') or item.get('title') or item.get('bsnsNm', '')).strip()
                link = item.get('pblancUrl') or item.get('link') or 'https://www.bizinfo.go.kr'
                notice_date = (item.get('reqstBgngDt') or item.get('regDate') or item.get('pblancBgnDe', today.strftime('%Y-%m-%d')))[:10]
                if not title: continue

                clean_title = title.replace(" ", "").lower()
                matched_keyword = next((kw for kw in keyword_list if kw in clean_title), None)
                title_key = f"중소벤처기업부_{clean_title}"

                if matched_keyword:
                    matched_count += 1
                    if link not in existing_links and title_key not in existing_titles:
                        existing_links.add(link)
                        existing_titles.add(title_key)
                        new_inserts.append({'agency': "중소벤처기업부", 'title': title, 'link': link, 'post_date': notice_date, 'matched_keyword': matched_keyword})

        print(f"🎯 중소벤처기업부 키워드 매칭 건수: {matched_count}건")
    except Exception as e: print(f"❌ 중소벤처기업부 API 예외 발생: {safe_error(e)}")

    # =========================================================================
    # [파트 4] 중소벤처기업부_사업공고 API 수집
    # =========================================================================
    try:
        print("🌐 중소벤처기업부_사업공고 API 요청 중...")
        req = requests.models.PreparedRequest()
        req.prepare_url(MSS_BIZ_ENDPOINT, {'serviceKey': SERVICE_KEY, 'numOfRows': '100', 'pageNo': '1'})
        res = session.get(req.url, timeout=30)

        items = []
        res_text = res.text.strip()
        if res_text.startswith('<'):
            try:
                for el in ET.fromstring(res.text).iter():
                    if el.tag in ['list', 'item']:
                        items.append({child.tag: (child.text or '') for child in el})
            except: pass
        else:
            try:
                items = res.json().get('response', {}).get('body', {}).get('items', [])
            except: pass

        print(f"📦 중소벤처기업부_사업공고에서 받아온 공고 수: {len(items)}건")

        matched_count = 0
        for item in items:
            title = (item.get('pblancNm') or item.get('title') or item.get('bsnsNm', '')).strip()
            link = item.get('pblancUrl') or item.get('link') or 'https://www.bizinfo.go.kr'
            notice_date = (item.get('reqstBgngDt') or item.get('regDate') or item.get('pblancBgnDe', today.strftime('%Y-%m-%d')))[:10]
            if not title: continue

            clean_title = title.replace(" ", "").lower()
            matched_keyword = next((kw for kw in keyword_list if kw in clean_title), None)
            title_key = f"중소벤처기업부_사업공고_{clean_title}"

            if matched_keyword:
                matched_count += 1
                if link not in existing_links and title_key not in existing_titles:
                    existing_links.add(link)
                    existing_titles.add(title_key)
                    new_inserts.append({'agency': "중소벤처기업부_사업공고", 'title': title, 'link': link, 'post_date': notice_date, 'matched_keyword': matched_keyword})

        print(f"🎯 중소벤처기업부_사업공고 키워드 매칭 건수: {matched_count}건")
    except Exception as e: print(f"❌ 중소벤처기업부_사업공고 API 예외 발생: {safe_error(e)}")

    # =========================================================================
    # [파트 5] 중소벤처24 API 수집
    # =========================================================================
    try:
        print("🌐 중소벤처24 API 요청 중...")
        encoded_token = urllib.parse.quote(SMES_API_TOKEN, safe="")
        str_dt, end_dt = (today - timedelta(days=7)).strftime('%Y%m%d'), today.strftime('%Y%m%d')
        url = f"{SMES_API_ENDPOINT}?token={encoded_token}&strDt={str_dt}&endDt={end_dt}&html=no"

        res = session.get(url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=30).json()
        inner_result = res.get('result', res)

        items = []
        if str(inner_result.get('resultCd')) == '0':
            items = inner_result.get('data', [])

        print(f"📦 중소벤처24에서 받아온 공고 수: {len(items)}건")

        matched_count = 0
        for item in items:
            title = item.get('pblancNm', '').strip()
            link = item.get('pblancDtlUrl') or 'https://www.smes.go.kr/smes24'
            raw_date = item.get('creatDt') or item.get('pblancBgnDt', '')
            notice_date = raw_date[:10] if raw_date else today.strftime('%Y-%m-%d')
            if not title: continue

            clean_title = title.replace(" ", "").lower()
            matched_keyword = next((kw for kw in keyword_list if kw in clean_title), None)
            title_key = f"중소벤처24_{clean_title}"

            if matched_keyword:
                matched_count += 1
                if link not in existing_links and title_key not in existing_titles:
                    existing_links.add(link)
                    existing_titles.add(title_key)
                    new_inserts.append({'agency': "중소벤처24", 'title': title, 'link': link, 'post_date': notice_date, 'matched_keyword': matched_keyword})

        print(f"🎯 중소벤처24 키워드 매칭 건수: {matched_count}건")
    except Exception as e: print(f"❌ 중소벤처24 API 예외 발생: {safe_error(e)}")

        # =========================================================================
    # 추가 API 공통 처리
    # =========================================================================
    def api_text(value):
        return re.sub(r"\s+", " ", str(value or "")).strip()

    def api_date(value):
        match = re.search(
            r"(20\d{2})[-./]?(\d{2})[-./]?(\d{2})",
            str(value or ""),
        )
        return (
            "-".join(match.groups())
            if match
            else today.strftime("%Y-%m-%d")
        )

    def api_error_text(value):
        text = str(value or "")
        for secret in (SERVICE_KEY, urllib.parse.unquote(SERVICE_KEY)):
            if secret:
                text = text.replace(secret, "[인증키 숨김]")
                text = text.replace(
                    urllib.parse.quote(secret, safe=""),
                    "[인증키 숨김]",
                )
        return api_text(text)[:250]

    def fetch_extra_api(client, endpoint, params, record_tag):
        response = client.get(
            endpoint,
            params=params,
            timeout=(8, 20),
        )

        status = response.status_code
        raw = response.text.strip()

        if status != 200:
            raise ValueError(
                f"HTTP {status}: {api_error_text(raw)}"
            )

        # JSON 응답
        if raw.startswith(("{", "[")):
            payload = response.json()
            envelope = payload.get("response", payload)
            header = envelope.get("header") or {}
            code = str(header.get("resultCode", ""))
            message = header.get("resultMsg", "")

            if code not in ("00", "0"):
                raise ValueError(
                    f"HTTP {status}, API 코드 {code or '없음'}: "
                    f"{api_error_text(message or raw)}"
                )

            body = envelope.get("body") or {}
            container = (
                body.get("data")
                if record_tag == "list"
                else body.get("items")
            )

            if isinstance(container, dict):
                records = container.get(record_tag) or []
            elif isinstance(container, list):
                records = container
            else:
                records = []

            if isinstance(records, dict):
                records = [records]

            records = [
                item for item in records
                if isinstance(item, dict)
            ]
            total = body.get("totalCount")

        # XML 응답（기본 형식）
        else:
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError:
                raise ValueError(
                    f"HTTP {status}, XML/JSON이 아닌 응답: "
                    f"{api_error_text(raw)}"
                ) from None

            for element in root.iter():
                element.tag = element.tag.rsplit("}", 1)[-1]

            code = api_text(root.findtext(".//resultCode"))
            message = (
                root.findtext(".//resultMsg")
                or root.findtext(".//returnAuthMsg")
                or root.findtext(".//errMsg")
                or ""
            )

            if code not in ("00", "0"):
                gateway_code = root.findtext(
                    ".//returnReasonCode", ""
                )
                raise ValueError(
                    f"HTTP {status}, API 코드 "
                    f"{code or gateway_code or '없음'}: "
                    f"{api_error_text(message or raw)}"
                )

            path = (
                ".//body/data/list"
                if record_tag == "list"
                else ".//body/items/item"
            )
            records = [
                {
                    child.tag: "".join(child.itertext()).strip()
                    for child in element
                }
                for element in root.findall(path)
            ]
            total = root.findtext(".//body/totalCount")

        total = (
            int(str(total).strip())
            if str(total).strip().isdigit()
            else None
        )
        return records, total

    def append_extra_notice(agency, title, link, raw_date,
                            check_link=False):
        title = api_text(title)
        if not title:
            return False

        clean_title = title.replace(" ", "").lower()
        keyword = next(
            (
                kw for kw in keyword_list
                if kw and kw in clean_title
            ),
            None,
        )
        if not keyword:
            return False

        title_key = f"{agency}_{clean_title}"

        if title_key in existing_titles:
            return False

        if check_link and link in existing_links:
            return False

        new_inserts.append({
            "agency": agency,
            "title": title,
            "link": link,
            "post_date": api_date(raw_date),
            "matched_keyword": keyword,
        })

        existing_titles.add(title_key)
        if check_link:
            existing_links.add(link)

        return True

    # 追加 API는 자동 재시도 없이 실행
    with requests.Session() as extra_session:
        extra_session.mount(
            "https://", HTTPAdapter(max_retries=0)
        )
        extra_session.mount(
            "http://", HTTPAdapter(max_retries=0)
        )


        # =====================================================================
        # [파트 6] 누리장터_민간입찰
        # =====================================================================
        nuri_agency = "누리장터_민간입찰"
        nuri_base = (
            "https://apis.data.go.kr/1230000/ao/"
            "PrvtBidNtceService"
        )
        nuri_services = [
            ("getPrvtBidPblancListInfoServc", "용역"),
            ("getPrvtBidPblancListInfoThng", "물품"),
            ("getPrvtBidPblancListInfoCnstwk", "공사"),
            ("getPrvtBidPblancListInfoEtc", "기타"),
        ]

        # 한국시간 기준 최근 7일 등록 공고.
        # GitHub Actions의 UTC 시간대에 영향받지 않도록 계산.
        nuri_now = datetime.utcnow() + timedelta(hours=9)
        nuri_begin = (
            nuri_now - timedelta(days=7)
        ).strftime("%Y%m%d0000")
        nuri_end = nuri_now.strftime("%Y%m%d%H%M")

        nuri_seen = set()
        nuri_total_added = 0

        for operation, label in nuri_services:
            received_count = 0

            for page_number in range(1, 4):
                try:
                    items, total = fetch_extra_api(
                        extra_session,
                        f"{nuri_base}/{operation}",
                        {
                            "ServiceKey": urllib.parse.unquote(
                                SERVICE_KEY
                            ),
                            "numOfRows": 100,
                            "pageNo": page_number,
                            "inqryDiv": "1",
                            "inqryBgnDt": nuri_begin,
                            "inqryEndDt": nuri_end,
                        },
                        "item",
                    )

                except requests.RequestException as error:
                    print(
                        f"❌ {nuri_agency} / {label}: "
                        f"접속 실패 ({type(error).__name__})",
                        flush=True,
                    )
                    break

                except ValueError as error:
                    print(
                        f"❌ {nuri_agency} / {label}: "
                        f"{api_error_text(error)}",
                        flush=True,
                    )
                    break

                if not items:
                    print(
                        f"↳ {nuri_agency} / {label}: "
                        "추가 데이터 없음",
                        flush=True,
                    )
                    break

                received_count += len(items)
                fresh_count = 0
                added_count = 0

                for item in items:
                    title = api_text(item.get("ntceNm"))
                    notice_no = api_text(item.get("bidNtceNo"))
                    notice_order = api_text(
                        item.get("bidNtceOrd")
                    )
                    record_id = (
                        f"{notice_no}_{notice_order}"
                        if notice_no
                        else title
                    )

                    if not record_id or record_id in nuri_seen:
                        continue

                    nuri_seen.add(record_id)
                    fresh_count += 1

                    # 문서에서 제공하는 실제 공고 첨부파일 URL.
                    document_link = next(
                        (
                            api_text(item.get(f"ntceSpecDocUrl{i}"))
                            for i in range(1, 11)
                            if api_text(
                                item.get(f"ntceSpecDocUrl{i}")
                            ).startswith(("https://", "http://"))
                        ),
                        "",
                    )

                    # 첨부파일 URL도 없는 경우 나라장터 홈페이지.
                    link = document_link or "https://www.g2b.go.kr/"

                    if append_extra_notice(
                        nuri_agency,
                        title,
                        link,
                        item.get("nticeDt") or item.get("rgstDt"),
                        check_link=bool(document_link),
                    ):
                        added_count += 1
                        nuri_total_added += 1

                print(
                    f"📄 {nuri_agency} / {label} "
                    f"{page_number}/3페이지: "
                    f"{len(items)}건 확인, 신규 {added_count}건",
                    flush=True,
                )

                if fresh_count == 0:
                    print(
                        f"⚠️ {nuri_agency} / {label}: "
                        "동일 목록 반복, 종료",
                        flush=True,
                    )
                    break

                if total is not None and received_count >= total:
                    break

                if page_number == 3:
                    print(
                        f"↳ {nuri_agency} / {label}: "
                        "최대 3페이지 수집 완료"
                        + (
                            f" (전체 {total}건 중 "
                            f"{received_count}건 확인)"
                            if total is not None
                            else ""
                        ),
                        flush=True,
                    )

        print(
            f"🎯 {nuri_agency}: 신규 "
            f"{nuri_total_added}건",
            flush=True,
        )

    # =========================================================================
    # [파트 7] Supabase 저장
    # =========================================================================
    if new_inserts:
        supabase.table("notices").insert(new_inserts).execute()
        print(
            f"✨ 총 {len(new_inserts)}건의 새로운 공고를 저장했습니다!",
            flush=True,
        )
    else:
        print(
            "⚠️ 조건에 맞는 새로운 공고가 없습니다.",
            flush=True,
        )


if __name__ == "__main__":
    run_scraper()
