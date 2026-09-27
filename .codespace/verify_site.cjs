// Проверка сайта в реальном браузере: панель загрузки прячется, появляется
// панель прохода с градиентом, в истории сверху in_progress, а по клику на
// проход открываются блоки анимаций.
const { chromium } = require("playwright");
const JSZip = require("jszip");

const SITE = process.env.SITE || "https://vladleopold.github.io/3Spine/";
const BROKER = "https://spine-broker.leopolds2010.workers.dev";
const JOB = "convert-testjob-0001";

function png1x1() {
  return Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "base64");
}

async function buildZip() {
  const z = new JSZip();
  z.file("previews/index.json", JSON.stringify({
    items: [
      { png: "previews/hero.png", spine: "res/hero.spine", name: "hero", bytes: 68, kind: "render" },
      { png: "previews/logo.png", spine: "res/logo.spine", name: "logo", bytes: 68, kind: "render" },
    ],
  }));
  z.file("previews/hero.png", png1x1());
  z.file("previews/logo.png", png1x1());
  z.file("res/hero.spine", "SPINE");
  z.file("res/hero.atlas", "hero.png\nsize: 1,1\n");
  z.file("res/hero.png", png1x1());
  z.file("res/logo.spine", "SPINE");
  z.file("res/logo.atlas", "logo.png\nsize: 1,1\n");
  z.file("res/logo.png", png1x1());
  return z.generateAsync({ type: "nodebuffer" });
}

(async () => {
  const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await chromium.launch(
    require("fs").existsSync(CHROME) ? { headless: true, executablePath: CHROME } : { headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const zip = await buildZip();
  let statusCalls = 0;
  const seen = [];

  await page.route("**/spine-broker.leopolds2010.workers.dev/**", async (route) => {
    const url = route.request().url();
    seen.push(url.replace(BROKER, ""));
    const json = (o) => route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(o) });
    if (url.includes("/convert")) return json({ job: JOB, mode: "url" });
    if (url.includes("/status")) {
      statusCalls++;
      return json({ ready: statusCalls > 2 });          // пауза, потом готовность
    }
    if (url.includes("/download")) {
      return route.fulfill({ status: 200, contentType: "application/zip", body: zip });
    }
    if (url.includes("/history")) {
      return json({ items: [
        { job: "convert-mujcow0b-47bhvm", file: "history/convert-mujcow0b-47bhvm.zip", date: "2026-09-27T08:03:11Z", bytes: 2400, spine: true },
      ]});
    }
    if (url.includes("/stats")) return json({ visits: 10 });
    if (url.includes("/visit")) return json({ ok: true });
    return json({});
  });

  const fails = [];
  const ok = (cond, name) => { console.log((cond ? "  OK   " : "  FAIL ") + name); if (!cond) fails.push(name); };

  console.log("1. Загрузка страницы");
  await page.goto(SITE, { waitUntil: "domcontentloaded" });
  await page.waitForTimeout(1200);
  ok(await page.locator("section.pick").isVisible(), "панель загрузки видна до старта");

  console.log("2. Start по ссылке");
  await page.fill("#src-url", "https://demogamesfree.pragmaticplay.net/gs2c/openGame.do?gameSymbol=vs20bgdoghouse");
  await page.waitForTimeout(200);
  await page.click("#start");
  await page.waitForTimeout(1200);

  const pickVisible = await page.locator("section.pick").isVisible();
  const runVisible = await page.locator("#run-panel").isVisible();
  const running = await page.evaluate(() => document.body.classList.contains("running"));
  const step = (await page.locator("#run-step").textContent() || "").trim();
  const elapsed = (await page.locator("#run-elapsed").textContent() || "").trim();
  const badge = (await page.locator("#run-badge").textContent() || "").trim();

  ok(!pickVisible, "панель drag-and-drop ПРОПАЛА после Start");
  ok(runVisible, "панель прохода показана");
  ok(running, "на фоне включён бегущий градиент (body.running)");
  ok(badge === "in_progress", "бейдж показывает in_progress (получено: " + badge + ")");
  ok(/\d/.test(elapsed), "идёт счётчик времени (" + elapsed + ")");
  ok(step.length > 3, "есть текст этапа (" + step.slice(0, 48) + "…)");

  console.log("3. in_progress в истории");
  const inProg = page.locator("#history-list .history-item .h-tag.in-progress");
  ok(await inProg.count() === 1, "в истории ровно один блок in_progress");
  ok((await inProg.textContent() || "").includes("in_progress"), "тег содержит in_progress");

  console.log("4. Завершение прохода и блоки анимаций");
  await page.waitForTimeout(9000);
  const cards = await page.locator("#previews-grid .pv-card").count();
  ok(await page.locator("#previews-panel").isVisible(), "панель превью видна");
  ok(cards === 2, "показаны блоки анимаций (получено " + cards + ")");
  ok(!(await page.evaluate(() => document.body.classList.contains("running"))), "градиент остановлен после готовности");

  console.log("5. Клик по проходу в истории открывает анимации");
  await page.click("#previews-back");
  await page.waitForTimeout(500);
  const histJob = page.locator("#history-list .history-item .h-job", { hasText: "convert-mujcow0b-47bhvm" });
  ok(await histJob.count() === 1, "старый проход есть в истории");
  await histJob.click();
  await page.waitForTimeout(2500);
  const cards2 = await page.locator("#previews-grid .pv-card").count();
  ok(await page.locator("#previews-panel").isVisible(), "открылась панель анимаций прохода");
  ok(cards2 === 2, "анимации прохода показаны (получено " + cards2 + ")");

  console.log("6. Скачивание одной пары (поштучно)");
  const dl = page.waitForEvent("download", { timeout: 15000 }).catch(() => null);
  await page.locator("#previews-grid .pv-card .btn").first().click();
  const file = await dl;
  ok(!!file, "клик по блоку анимации запускает скачивание (" + (file ? await file.suggestedFilename() : "нет файла") + ")");

  console.log("\nAPI вызовы: " + seen.slice(0, 8).join(", "));
  console.log(fails.length ? "\nПРОВАЛЕНО: " + fails.length + " → " + fails.join("; ") : "\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ");
  await browser.close();
  process.exit(fails.length ? 1 : 0);
})();
