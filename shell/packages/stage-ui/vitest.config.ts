import { resolve } from 'node:path'
import { cwd } from 'node:process'

import Vue from '@vitejs/plugin-vue'
import Info from 'unplugin-info/vite'

import { playwright } from '@vitest/browser-playwright'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

/**
 * Chromium reports this when a ResizeObserver pass leaves notifications for the next frame.
 * Virtua does this by design for long lists, so browser tests hide only this console message.
 */
const RESIZE_OBSERVER_LOOP_MESSAGE = 'ResizeObserver loop completed with undelivered notifications'

export default defineConfig({
  root: import.meta.dirname,
  server: {
    host: '127.0.0.1',
    // Vite 8 enables this only when it detects a coding agent. It repeats browser console
    // errors and window errors that Vitest already reports, and it cannot filter messages.
    forwardConsole: false,
  },
  resolve: { dedupe: ['vue'] },
  optimizeDeps: { entries: ['src/**/*.browser.test.ts'] },
  plugins: [
    Info(),
    Vue(),
  ],
  test: {
    env: loadEnv('test', cwd(), ''),
    // NOTICE:
    // Virtua measures rows inside its ResizeObserver callback. When a long list first gets
    // real row sizes, it enables its buffer and mounts more rows in that same callback, so
    // Chromium delivers their first notifications one frame later and the browser project's
    // window error catcher logs this message. No notification is lost.
    // The chat history browser test counts the message separately to guard its own observers.
    // Vitest reads this hook from the root test config only, not from a project.
    // Source: node_modules/virtua/src/core/store.ts ACTION_ITEM_RESIZE,
    // https://github.com/inokawa/virtua/issues/470, and the Virtua README FAQ.
    // Remove this when Virtua defers range updates that follow row measurement.
    onConsoleLog: log => !log.includes(RESIZE_OBSERVER_LOOP_MESSAGE),
    projects: [
      {
        extends: true,
        test: {
          name: 'node',
          setupFiles: ['./src/test/setup-local-storage.ts'],
          include: ['src/**/*.test.ts'],
          exclude: ['src/**/*.browser.test.ts'],
          fileParallelism: false,
          hookTimeout: 20_000,
          maxWorkers: 1,
          testTimeout: 20_000,
        },
      },
      {
        extends: true,
        test: {
          name: 'browser',
          include: ['src/**/*.browser.test.ts'],
          exclude: ['**/node_modules/**'],
          browser: {
            enabled: true,
            headless: true,
            screenshotDirectory: resolve(import.meta.dirname, '../../../tests/output/shell-s3/screenshots/ui'),
            provider: playwright(),
            instances: [
              { browser: 'chromium' },
            ],
          },
        },
      },
    ],
  },
})
