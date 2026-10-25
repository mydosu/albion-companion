// Requires Windows, a desktop session, a GPU, and shell's installed Playwright dependency.
import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { mkdir, rename, writeFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { createServer } from 'node:net'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { setTimeout as delay } from 'node:timers/promises'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const requireBrowser = createRequire(join(root, 'shell/node_modules/@vitest/browser-playwright/package.json'))
const { _electron: electron } = requireBrowser('playwright')
const bundle = resolve(process.argv[2])
const output = resolve(process.argv[3] || join(root, 'tests/output/shell-s3/smoke'))
assert.ok(output.replaceAll('\\', '/').includes('/tests/output/'))
await mkdir(output, { recursive: true })

function processes() {
  const escaped = bundle.replaceAll("'", "''")
  const command = `$items = @(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith('${escaped}\\', [System.StringComparison]::OrdinalIgnoreCase) } | Select-Object ProcessId,ParentProcessId,ExecutablePath); ConvertTo-Json -InputObject $items -Compress`
  return JSON.parse(execFileSync('powershell.exe', ['-NoProfile', '-Command', command], { encoding: 'utf8', windowsHide: true }).trim() || '[]')
}

async function waitFor(check, description) {
  for (let attempt = 0; attempt < 60; attempt++) {
    if (await check())
      return
    await delay(500)
  }
  throw new Error(`Timed out: ${description}`)
}

async function unusedPort() {
  const server = createServer()
  await new Promise(resolveListen => server.listen(0, '127.0.0.1', resolveListen))
  const port = server.address().port
  await new Promise(resolveClose => server.close(resolveClose))
  return String(port)
}

// The router rewrites the hash during boot, so set the route until it sticks.
async function openModelsRoute(page) {
  for (let attempt = 0; attempt < 20; attempt++) {
    await page.evaluate(() => { window.location.hash = '/settings/models' })
    try {
      await page.waitForFunction(() => window.location.hash === '#/settings/models', null, { timeout: 2_000 })
      return
    }
    catch {}
    await delay(500)
  }
  throw new Error('Cannot open the models settings route')
}

async function launch(name) {
  const profile = join(output, name)
  await mkdir(profile, { recursive: true })
  const env = { ...process.env, ALBION_SIDECARS: '0', APP_USER_DATA_PATH: profile, SERVER_CHANNEL_PORT: await unusedPort() }
  delete env.ELECTRON_RUN_AS_NODE
  delete env.GODOT4
  delete env.DOTNET_ROOT
  delete env.APP_REMOTE_DEBUG
  const application = await electron.launch({
    executablePath: join(bundle, 'airi.exe'),
    args: [`--user-data-dir=${profile}`],
    env,
    cwd: output,
    timeout: 60_000,
  })
  try {
    assert.equal(await application.evaluate(({ app }) => app.isPackaged), true)
    // The shell also opens caption and chatbox windows; only the leader window hosts the settings pages.
    const leader = () => application.windows()
      .filter(window => window.url().includes('index.html'))
      .find(window => window.url().includes('synced-leader=true'))
    await waitFor(() => leader() !== undefined, 'main window')
    const page = leader()
    await openModelsRoute(page)
    await page.getByRole('button', { name: 'Switch to Godot Stage (Experimental)', exact: true }).waitFor({ timeout: 45_000 })
    return { application, page }
  }
  catch (error) {
    await application.close().catch(() => {})
    throw error
  }
}

const result = { before: processes(), checks: [] }
assert.equal(result.before.length, 0, 'Only run this probe against an idle test bundle.')
const sidecars = () => processes().filter(item => item.ExecutablePath.toLowerCase().endsWith('godot-stage.exe'))
let current
try {
  current = await launch('complete-profile')
  await current.page.getByRole('button', { name: 'Switch to Godot Stage (Experimental)', exact: true }).click()
  await current.page.getByRole('button', { name: 'Back to Built-in Stage', exact: true }).waitFor({ timeout: 30_000 })
  assert.equal(sidecars().length, 1)
  result.checks.push('packaged sidecar started and reported ready without GODOT4 or DOTNET_ROOT')
  await current.page.screenshot({ path: join(output, 'running.png') })
  await current.page.getByRole('button', { name: 'Back to Built-in Stage', exact: true }).click()
  await waitFor(() => sidecars().length === 0, 'sidecar stop')
  result.checks.push('toggle stop leaves zero sidecars')
  await current.page.getByRole('button', { name: 'Switch to Godot Stage (Experimental)', exact: true }).click()
  await current.page.getByRole('button', { name: 'Back to Built-in Stage', exact: true }).waitFor({ timeout: 30_000 })
  result.running = processes()
  assert.equal(sidecars().length, 1)
  const application = current.application
  const closed = application.waitForEvent('close', { timeout: 30_000 })
  await application.evaluate(({ app }) => { setTimeout(() => app.quit(), 0) })
  await closed
  current = undefined
  await waitFor(() => processes().length === 0, 'normal app quit and child cleanup')
  result.checks.push('normal Electron quit leaves zero app and sidecar processes')

  const executable = join(bundle, 'resources/godot-stage/godot-stage.exe')
  const hidden = `${executable}.smoke-hidden`
  await rename(executable, hidden)
  try {
    current = await launch('missing-profile')
    await current.page.getByRole('button', { name: 'Switch to Godot Stage (Experimental)', exact: true }).click()
    await current.page.getByText(/Reinstall the complete application bundle/).waitFor({ timeout: 15_000 })
    assert.equal(sidecars().length, 0)
    assert.equal(await current.page.getByRole('button', { name: 'Switch to Godot Stage (Experimental)', exact: true }).count(), 1)
    result.checks.push('missing sidecar reports recovery hint and keeps the built-in renderer')
    await current.page.screenshot({ path: join(output, 'missing.png') })
    await current.application.close()
    current = undefined
  }
  finally {
    await rename(hidden, executable)
  }
  await waitFor(() => processes().length === 0, 'missing-sidecar test cleanup')
  result.after = processes()
  result.passed = result.checks.length
  result.failed = 0
}
catch (error) {
  result.failed = 1
  result.error = error.message
  throw error
}
finally {
  if (current)
    await current.application.close().catch(() => {})
  result.finalProcesses = processes()
  await writeFile(join(output, 'result.json'), JSON.stringify(result, null, 2))
  console.log(JSON.stringify({ passed: result.passed, failed: result.failed, residual: result.finalProcesses.length }))
}
