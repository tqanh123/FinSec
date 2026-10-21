// render-check — bố cục ở nhiều cỡ màn hình (headless Chrome thật). Khác
// e2e_browser.mjs (functional: ảnh/lang/router ở MỘT viewport): đây quét
// horizontal-overflow (scrollWidth > viewport width) ở NHIỀU viewport —
// dấu hiệu bố cục vỡ responsive phổ biến nhất. Báo lỗi dạng text, không im lặng.
//
//   NODE_PATH=harness/state/e2e-node/node_modules \
//     node render_check.mjs [--public public] [--viewports 375x667,768x1024,1440x900] [--shots <dir>]
//
// Exit: 0 sạch · 1 có lỗi bố cục · 2 không khởi động được Chrome.
import { createRequire } from "module";
import { createServer } from "http";
import { readFileSync, existsSync, mkdirSync, readdirSync } from "fs";
import { join, extname, resolve } from "path";

const require = createRequire(import.meta.url);
const puppeteer = require("puppeteer-core");

const CHROME = process.env.CHROME_BIN || "/usr/bin/google-chrome";
const args = process.argv.slice(2);
const arg = (flag, dflt) => args.includes(flag) ? args[args.indexOf(flag) + 1] : dflt;
const PUBLIC = resolve(arg("--public", "public"));
const shotsArg = arg("--shots", null);
const VIEWPORTS = arg("--viewports", "375x667,768x1024,1440x900").split(",").map((v) => {
  const [width, height] = v.split("x").map(Number);
  return { width, height };
});

const MIME = { ".html": "text/html", ".css": "text/css", ".js": "text/javascript",
  ".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".json": "application/json",
  ".woff2": "font/woff2", ".ico": "image/x-icon" };

function serve(root) {
  return new Promise((res) => {
    const srv = createServer((req, rq) => {
      let p = decodeURIComponent(req.url.split("?")[0]);
      if (p.endsWith("/")) p += "index.html";
      const fp = join(root, p);
      if (!fp.startsWith(root) || !existsSync(fp)) { rq.writeHead(404); return rq.end("404"); }
      rq.writeHead(200, { "content-type": MIME[extname(fp)] || "application/octet-stream" });
      rq.end(readFileSync(fp));
    });
    srv.listen(0, "127.0.0.1", () => res(srv));
  });
}

function pages(root) {
  const out = ["index.html"];
  const pdir = join(root, "pages");
  if (existsSync(pdir)) for (const f of readdirSync(pdir)) if (f.endsWith(".html")) out.push("pages/" + f);
  return out;
}

const fails = [];
function fail(pg, vp, msg) { fails.push(`${pg} @ ${vp.width}x${vp.height}: ${msg}`); }

const srv = await serve(PUBLIC);
const port = srv.address().port;
const base = `http://127.0.0.1:${port}`;
if (shotsArg) mkdirSync(shotsArg, { recursive: true });

let browser;
try {
  browser = await puppeteer.launch({
    executablePath: CHROME, headless: "new",
    args: ["--no-sandbox", "--disable-dev-shm-usage",
           "--enable-unsafe-swiftshader", "--use-gl=swiftshader"],
  });
} catch (e) {
  console.error("RENDER-CHECK: không khởi động được Chrome:", e.message);
  srv.close(); process.exit(2);
}

const allPages = pages(PUBLIC);
for (const pg of allPages) {
  for (const vp of VIEWPORTS) {
    const page = await browser.newPage();
    await page.setViewport(vp);
    try {
      // domcontentloaded, not networkidle0: layout only needs DOM+CSS settled, and a
      // showcase page with a running hero animation (canvas/WebGL, per e2e_browser.mjs's
      // own BENIGN list) never GOES network-idle — measured: networkidle0 here stalled
      // every page to its own 30s timeout, ~27min for this showcase's page x viewport count.
      const resp = await page.goto(`${base}/${pg}`, { waitUntil: "domcontentloaded", timeout: 15000 });
      if (!resp || !resp.ok()) { fail(pg, vp, `HTTP ${resp ? resp.status() : "no-response"}`); continue; }
      await new Promise((r) => setTimeout(r, 300)); // let CSS/fonts settle before measuring

      // Bố cục vỡ phổ biến nhất ở responsive: nội dung tràn NGANG khỏi viewport.
      const scrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);
      if (scrollWidth > vp.width + 1) {
        fail(pg, vp, `tràn ngang: scrollWidth=${scrollWidth} > viewport=${vp.width}`);
      }
      if (shotsArg) {
        await page.screenshot({
          path: join(shotsArg, `${pg.replace(/\//g, "_")}_${vp.width}x${vp.height}.png`),
          fullPage: true,
        });
      }
    } catch (e) {
      fail(pg, vp, `exception: ${e.message}`);
    } finally {
      // A page.close() on an already-dying connection (host under a timeout SIGTERM,
      // for instance) throws — never let a close failure crash the whole batch and
      // hide every fail() collected so far behind an uncaught rejection.
      await page.close().catch(() => {});
    }
  }
}

await browser.close();
srv.close();

console.log(`render-check: ${allPages.length} page x ${VIEWPORTS.length} viewport · ${PUBLIC}`);
if (fails.length) {
  console.error(`RENDER-CHECK: FAIL (${fails.length})`);
  for (const f of fails) console.error("  ✗ " + f);
  process.exit(1);
}
console.log("RENDER-CHECK: PASS");
