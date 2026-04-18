import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { chromium } from "playwright";

function parseArgs(argv) {
  const [command, ...rest] = argv;
  const args = { _: command };
  for (let i = 0; i < rest.length; i += 2) {
    const key = rest[i];
    const value = rest[i + 1];
    if (!key?.startsWith("--")) {
      throw new Error(`Unexpected argument: ${key}`);
    }
    args[key.slice(2)] = value;
  }
  return args;
}

function boolValue(value, defaultValue = true) {
  if (value == null) {
    return defaultValue;
  }
  return String(value).toLowerCase() === "true";
}

async function fetchUrl(args) {
  const browser = await chromium.launch({
    headless: boolValue(args["headless"], true),
    executablePath: args["chrome-executable"],
  });
  const context = await browser.newContext({
    storageState: args["storage-state"],
  });
  const page = await context.newPage();
  await page.goto(args.url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(1500);
  const payload = {
    html: await page.content(),
    title: await page.title(),
    url: page.url(),
  };
  await context.close();
  await browser.close();
  process.stdout.write(JSON.stringify(payload));
}

async function bootstrapFromProfile(args) {
  const userDataDir = args["chrome-user-data-dir"];
  const profileDirectory = args["chrome-profile-directory"] || "Default";
  const tempUserDataDir = await fs.mkdtemp(path.join(os.tmpdir(), "bib-agent-chrome-"));
  try {
    await cloneChromeProfile(userDataDir, tempUserDataDir, profileDirectory);
  } catch (error) {
    await fs.rm(tempUserDataDir, { recursive: true, force: true });
    throw error;
  }

  const context = await chromium.launchPersistentContext(tempUserDataDir, {
    headless: boolValue(args["headless"], true),
    executablePath: args["chrome-executable"],
    args: [`--profile-directory=${profileDirectory}`],
  });

  const page = context.pages()[0] || (await context.newPage());
  await page.goto(args.url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.waitForTimeout(2000);

  const html = await page.content();
  if (html.includes("Sign in") && !html.includes("gsc_a_tr")) {
    await context.close();
    await fs.rm(tempUserDataDir, { recursive: true, force: true });
    throw new Error(
      "Chrome profile does not appear to have an active Scholar session. " +
        "Log into Google Scholar in that Chrome profile first and rerun auth-bootstrap."
    );
  }

  const storageStatePath = args["storage-state"];
  await fs.mkdir(path.dirname(storageStatePath), { recursive: true });
  await context.storageState({ path: storageStatePath });
  await context.close();
  await fs.rm(tempUserDataDir, { recursive: true, force: true });
  process.stdout.write(
    JSON.stringify({
      ok: true,
      storageStatePath,
      url: page.url(),
    })
  );
}

async function cloneChromeProfile(sourceUserDataDir, targetUserDataDir, profileDirectory) {
  const namesToCopy = [
    "Local State",
    "First Run",
    profileDirectory,
  ];

  for (const name of namesToCopy) {
    const source = path.join(sourceUserDataDir, name);
    const target = path.join(targetUserDataDir, name);
    try {
      const stat = await fs.stat(source);
      if (stat.isDirectory()) {
        await fs.cp(source, target, {
          recursive: true,
          filter: (src) => {
            const base = path.basename(src);
            return ![
              "SingletonCookie",
              "SingletonLock",
              "SingletonSocket",
              "lockfile",
            ].includes(base);
          },
        });
      } else {
        await fs.copyFile(source, target);
      }
    } catch (error) {
      if (error && error.code === "ENOENT") {
        continue;
      }
      throw error;
    }
  }
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args._ === "fetch-url") {
    await fetchUrl(args);
    return;
  }
  if (args._ === "bootstrap-from-profile") {
    await bootstrapFromProfile(args);
    return;
  }
  throw new Error(`Unsupported command: ${args._}`);
}

main().catch((error) => {
  process.stderr.write(String(error.stack || error));
  process.exit(1);
});
