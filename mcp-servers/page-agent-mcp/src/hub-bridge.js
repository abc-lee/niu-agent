#!/usr/bin/env node
import { readFileSync } from 'node:fs'
import http from 'node:http'
import { fileURLToPath } from 'node:url'
import { WebSocketServer } from 'ws'

const EXT_ID = 'akldabonmimlicnjlflnapfeklbfemhj'
const STORE_URL = `https://chromewebstore.google.com/detail/page-agent-ext/${EXT_ID}`
const LOOPBACK_HOST = 'localhost'

const launcherTemplate = readFileSync(
	fileURLToPath(new URL('./launcher.html', import.meta.url)),
	'utf-8'
)

/**
 * HTTP + WebSocket bridge to the hub.html extension tab.
 * - HTTP serves the launcher page (triggers extension to open hub)
 * - WS carries execute/stop commands and result/error responses
 */
export class HubBridge {
	/** @type {number} */
	port

	/** @type {http.Server} */
	#httpServer

	/** @type {WebSocketServer} */
	#wss

	/** @type {import('ws').WebSocket | null} */
	#hub = null

	/** @type {{ resolve: (r: {success: boolean, data: string}) => void, reject: (e: Error) => void } | null} */
	#pendingTask = null

	/** @param {number} port */
	constructor(port) {
		this.port = port
		this.#httpServer = http.createServer((_req, res) => {
			const html = launcherTemplate
				.replaceAll('__EXT_ID__', EXT_ID)
				.replaceAll('__STORE_URL__', STORE_URL)
				.replaceAll('__WS_PORT__', String(port))
			res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
			res.end(html)
		})
		this.#wss = new WebSocketServer({ server: this.#httpServer })
		this.#wss.on('connection', (ws) => this.#onConnection(ws))
	}

	/** @returns {Promise<void>} */
	async start() {
		return new Promise((resolve, reject) => {
			this.#httpServer.on('error', (/** @type {NodeJS.ErrnoException} */ err) => {
				if (err.code === 'EADDRINUSE') {
					reject(
						new Error(`Port ${this.port} is in use. Another Page Agent MCP server may be running.`)
					)
				} else {
					reject(err)
				}
			})
			this.#httpServer.listen(this.port, LOOPBACK_HOST, () => {
				console.error(`[page-agent-mcp] HTTP + WS on http://${LOOPBACK_HOST}:${this.port}`)
				resolve()
			})
		})
	}

	get connected() {
		return this.#hub?.readyState === 1
	}

	get busy() {
		return this.#pendingTask !== null
	}

	/**
	 * 等待连接建立
	 * @param {number} timeoutMs - 超时时间（毫秒）
	 * @returns {Promise<void>}
	 */
	async waitForConnection(timeoutMs) {
		const startTime = Date.now()
		while (!this.connected && (Date.now() - startTime) < timeoutMs) {
			await new Promise(resolve => setTimeout(resolve, 500))
		}
	}

	/**
	 * @param {string} task
	 * @param {Record<string, unknown>} [config]
	 * @returns {Promise<{success: boolean, data: string}>}
	 */
	async executeTask(task, config) {
		// 如果未连接，尝试打开浏览器并等待连接
		if (!this.connected) {
			console.error('[hub-bridge] Hub not connected, attempting to open browser...')

			// 打开浏览器到 launcher 页面
			const { exec } = require('child_process')
			const { platform } = require('os')
			const url = `http://localhost:${this.port}`
			let cmd
			if (platform() === 'darwin') {
				cmd = 'open'
			} else if (platform() === 'win32') {
				cmd = 'start ""'
			} else if (platform() === 'linux') {
				cmd = 'xdg-open'
			} else {
				console.error(`[hub-bridge] Unsupported platform: ${platform()}, please open browser manually: ${url}`)
				// 继续等待连接（用户可能手动打开）
			}

			if (cmd) {
				exec(`${cmd} "${url}"`, (err) => {
					if (err) console.error('[hub-bridge] Failed to open browser:', err.message)
				})
			}

			// 等待连接建立（最多10秒）
			await this.waitForConnection(10000)
		}

		if (!this.connected) {
			throw new Error('Hub is not connected after waiting 10s. Is the extension running?')
		}

		if (this.#pendingTask) throw new Error('Agent is already running a task.')

		// 添加超时保护（2分钟）
		return new Promise((resolve, reject) => {
			const timeout = setTimeout(() => {
				this.#pendingTask = null
				reject(new Error('Task execution timed out after 120s'))
			}, 120000)

			this.#pendingTask = {
				resolve: (r) => {
					clearTimeout(timeout)
					resolve(r)
				},
				reject: (e) => {
					clearTimeout(timeout)
					reject(e)
				}
			}
			this.#hub.send(JSON.stringify({ type: 'execute', task, config }))
		})
	}

	stopTask() {
		if (this.connected) {
			this.#hub.send(JSON.stringify({ type: 'stop' }))
		}
		// 清理 pendingTask，避免 busy 状态卡住
		if (this.#pendingTask) {
			this.#pendingTask.reject(new Error('Task stopped by user'))
			this.#pendingTask = null
		}
	}

	// TODO: Add version checking

	/** @param {import('ws').WebSocket} ws */
	#onConnection(ws) {
		if (this.#hub && this.#hub.readyState === 1) {
			ws.close(4000, 'Another hub is already connected')
			return
		}

		this.#hub = ws
		console.error('[page-agent-mcp] Hub connected')

		ws.on('message', (/** @type {Buffer} */ rawData) => {
			/** @type {{ type: string, success?: boolean, data?: string, message?: string }} */
			let msg
			try {
				msg = JSON.parse(rawData.toString('utf-8'))
			} catch {
				return
			}

			if (msg.type === 'result') {
				this.#pendingTask?.resolve({ success: msg.success ?? false, data: msg.data ?? '' })
				this.#pendingTask = null
			} else if (msg.type === 'error') {
				this.#pendingTask?.reject(new Error(msg.message ?? 'Unknown error from hub'))
				this.#pendingTask = null
			}
		})

		ws.on('close', () => {
			console.error('[page-agent-mcp] Hub disconnected')
			if (this.#hub === ws) this.#hub = null
			if (this.#pendingTask) {
				this.#pendingTask.reject(new Error('Hub disconnected while task was running'))
				this.#pendingTask = null
			}
		})
	}
}
