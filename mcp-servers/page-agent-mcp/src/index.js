#!/usr/bin/env node
/**
 * Page-Agent MCP Server - ai-bot Integration
 *
 * 修改版：在没有环境变量时，fallback 读取 user-config.json
 * 最小改动原则：只改了这一个文件，其他来自上游包
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import { exec } from 'node:child_process'
import { platform, arch } from 'node:os'
import http from 'node:http'
import * as z from 'zod/v4'
import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

import { HubBridge } from './hub-bridge.js'

const env = process.env
const port = parseInt(env.PORT || '38401')

// ============== ai-bot 配置读取 ==============

/**
 * 尝试读取 user-config.json 作为 fallback
 * 向上查找项目根目录的 config/user-config.json
 */
function loadUserConfig() {
    // 获取当前文件的目录
    const __filename = fileURLToPath(import.meta.url)
    const __dirname = path.dirname(__filename)

    // 向上查找项目根目录：mcp-servers/page-agent-mcp/src -> ... -> config/user-config.json
    // 典型路径：.../mcp-servers/page-agent-mcp/src/../../../../../config/user-config.json
    // 简化：从 src 向上 5 层是项目根目录
    let configPath = path.resolve(__dirname, '../../../../../config/user-config.json')

    // 如果找不到，尝试其他可能的路径
    const possiblePaths = [
        configPath,
        path.resolve(__dirname, '../../../../config/user-config.json'),
        path.resolve(__dirname, '../../../config/user-config.json'),
        path.resolve(__dirname, '../../config/user-config.json'),
    ]

    for (const p of possiblePaths) {
        if (existsSync(p)) {
            configPath = p
            break
        }
    }

    try {
        if (existsSync(configPath)) {
            const config = JSON.parse(readFileSync(configPath, 'utf-8'))
            console.error(`[page-agent-mcp] Loaded config from: ${configPath}`)

            if (config.llm) {
                return {
                    apiKey: config.llm.apiKey,
                    // 优先使用 openaiCompatibleApiBase，否则使用 apiBase
                    baseURL: config.llm.openaiCompatibleApiBase || config.llm.apiBase,
                    model: config.llm.model,
                }
            }
        }
    } catch (e) {
        console.error(`[page-agent-mcp] Failed to read user-config.json: ${e.message}`)
    }

    return null
}

// ============== LLM 配置 ==============

/** @type {Record<string, string>} */
const llmConfig = {}

// 优先使用环境变量
if (env.LLM_BASE_URL) llmConfig.baseURL = env.LLM_BASE_URL
if (env.LLM_MODEL_NAME) llmConfig.model = env.LLM_MODEL_NAME
if (env.LLM_API_KEY) llmConfig.apiKey = env.LLM_API_KEY

// 如果环境变量不完整，fallback 到 user-config.json
if (!llmConfig.apiKey || !llmConfig.baseURL) {
    const userConfig = loadUserConfig()
    if (userConfig) {
        if (!llmConfig.apiKey && userConfig.apiKey) {
            llmConfig.apiKey = userConfig.apiKey
            console.error('[page-agent-mcp] Using API key from user-config.json')
        }
        if (!llmConfig.baseURL && userConfig.baseURL) {
            llmConfig.baseURL = userConfig.baseURL
            console.error('[page-agent-mcp] Using base URL from user-config.json')
        }
        if (!llmConfig.model && userConfig.model) {
            llmConfig.model = userConfig.model
            console.error('[page-agent-mcp] Using model from user-config.json')
        }
    }
}

// 验证配置
if (!llmConfig.apiKey) {
    console.error('[page-agent-mcp] WARNING: No LLM_API_KEY configured')
}
if (!llmConfig.baseURL) {
    console.error('[page-agent-mcp] WARNING: No LLM_BASE_URL configured')
}

// ============== Hub Bridge ==============

const hub = new HubBridge(port)
try {
    await hub.start()
    // Open launcher in default browser (only if we started our own hub)
    const url = `http://localhost:${port}`
    let cmd
    if (platform() === 'darwin') {
        cmd = 'open'
    } else if (platform() === 'win32') {
        // Windows: use start with empty title to avoid "start" being interpreted as window title
        cmd = 'start ""'
    } else {
        cmd = 'xdg-open'
    }
    exec(`${cmd} "${url}"`, (err) => {
        if (err) console.error(`[page-agent-mcp] Could not open browser: ${err.message}`)
    })
} catch (err) {
    if (err.message.includes('EADDRINUSE')) {
        console.error(`[page-agent-mcp] Port ${port} already in use - another instance may be running`)
        console.error(`[page-agent-mcp] MCP tools will use existing hub if available`)
    } else {
        throw err
    }
}

// ============== HTTP REST API for Python Client ==============

const API_PORT = parseInt(env.API_PORT || '38402')
const apiServer = http.createServer(async (req, res) => {
    // CORS headers
    res.setHeader('Access-Control-Allow-Origin', '*')
    res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS')
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type')

    if (req.method === 'OPTIONS') {
        res.writeHead(200)
        res.end()
        return
    }

    const url = new URL(req.url, `http://localhost:${API_PORT}`)

    // POST /execute - execute task
    if (req.method === 'POST' && url.pathname === '/execute') {
        let body = ''
        req.on('data', chunk => body += chunk)
        req.on('end', async () => {
            try {
                const { task } = JSON.parse(body)

                // 强制使用我们的代理配置（覆盖扩展自己的配置）
                const proxyConfig = {
                    baseURL: 'http://localhost:9876/proxy/v1',
                    model: 'local',
                    apiKey: 'local'
                }

                const result = await hub.executeTask(task, proxyConfig)

                res.writeHead(200, { 'Content-Type': 'application/json' })
                res.end(JSON.stringify({
                    success: result.success,
                    data: result.data
                }))
            } catch (err) {
                res.writeHead(500, { 'Content-Type': 'application/json' })
                res.end(JSON.stringify({ error: err.message }))
            }
        })
        return
    }

    // GET /status - get hub status
    if (req.method === 'GET' && url.pathname === '/status') {
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({
            connected: hub.connected,
            busy: hub.busy
        }))
        return
    }

    // POST /stop - stop task
    if (req.method === 'POST' && url.pathname === '/stop') {
        hub.stopTask()
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ message: 'Stop signal sent' }))
        return
    }

    // 404 for unknown routes
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ error: 'Not found' }))
})

apiServer.listen(API_PORT, 'localhost', () => {
    console.error(`[page-agent-mcp] REST API on http://localhost:${API_PORT}`)
})

// ============== MCP Server ==============

const mcpServer = new McpServer({ name: 'page-agent', version: '1.5.8-ai-bot' })

mcpServer.registerTool(
    'execute_task',
    {
        description: "Execute a task in user's browser. The task description should be specific and include the expected result.",
        inputSchema: {
            task: z
                .string()
                .describe(
                    'Task description in natural language. Give specific instructions for the task. Steps are preferable. Include the information you want to get after the task is done.'
                ),
        },
    },
    async ({ task }) => {
        try {
            const config = Object.keys(llmConfig).length > 0 ? llmConfig : undefined
            const result = await hub.executeTask(task, config)
            return {
                content: [
                    {
                        type: 'text',
                        text: result.success
                            ? `Task completed.\n\n${result.data}`
                            : `Task failed.\n\n${result.data}`,
                    },
                ],
            }
        } catch (err) {
            return {
                content: [{ type: 'text', text: `Error: ${err.message}` }],
                isError: true,
            }
        }
    }
)

mcpServer.registerTool(
    'get_status',
    {
        description: 'Check the current status of the Page Agent hub. Returns { connected, busy }.',
    },
    async () => ({
        content: [
            {
                type: 'text',
                text: JSON.stringify({ connected: hub.connected, busy: hub.busy }, null, 2),
            },
        ],
    })
)

mcpServer.registerTool(
    'stop_task',
    {
        description: 'Stop the currently running browser automation task.',
    },
    async () => {
        hub.stopTask()
        return { content: [{ type: 'text', text: 'Stop signal sent.' }] }
    }
)

const transport = new StdioServerTransport()
await mcpServer.connect(transport)
console.error('[page-agent-mcp] MCP server ready (stdio)')
