#!/usr/bin/env node
/**
 * Page-Agent MCP Server - ai-bot Integration
 *
 * 修改版：在没有环境变量时，fallback 读取 user-config.json
 * 支持动态系统提示词注入（多种工作模式）
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

// ============================================================================
// 系统提示词模板（支持多种工作模式）
// ============================================================================

const SYSTEM_PROMPTS = {
    // 知识库增强模式（默认）
    knowledge_enhanced: `
你是一个智能浏览器助手，具备本地知识库访问能力。

## 已注入的知识库内容

任务描述中已经包含了相关知识库内容，你可以直接使用这些信息：
- 知识库内容会以 "【知识库参考】" 开头
- 包含相关的背景信息、定义、操作指南等
- 请根据这些信息完成任务

## 工作原则

1. **快速反馈**：每步操作及时返回结果
2. **遇困即报**：遇到困难立即报告，不要长时间重试
3. **任务拆分**：复杂任务自动分解为小步骤
4. **知识优先**：优先使用已注入的知识库内容

## 语言

Default working language: **中文**
按用户使用的语言回复。
`
}

// ============================================================================

/**
 * 查询知识库并返回相关内容
 * @param {string} query - 查询关键词
 * @returns {Promise<string|null>} 知识库内容，失败返回 null
 */
async function queryKnowledgeBase(query) {
    const KB_API_BASE = 'http://localhost:9876/kb'

    try {
        const url = `${KB_API_BASE}/search?q=${encodeURIComponent(query)}&limit=3`
        const response = await fetch(url, {
            method: 'GET',
            headers: { 'Accept': 'application/json' },
            signal: AbortSignal.timeout(3000) // 3秒超时
        })

        if (!response.ok) {
            console.error(`[kb-query] HTTP ${response.status}`)
            return null
        }

        const data = await response.json()

        if (data.success && data.results && data.results.length > 0) {
            // 格式化知识库结果
            const knowledge = data.results
                .map((r, i) => `${i + 1}. ${r.title}\n${r.content}`)
                .join('\n\n')

            console.error(`[kb-query] Found ${data.results.length} results for: ${query}`)
            return knowledge
        }

        console.error(`[kb-query] No results for: ${query}`)
        return null
    } catch (error) {
        console.error(`[kb-query] Failed: ${error.message}`)
        return null
    }
}

/**
 * 从任务描述中提取知识库查询关键词
 * @param {string} task - 任务描述
 * @returns {string[]} 查询关键词列表
 */
function extractKnowledgeQueries(task) {
    const queries = []

    // 常见的专业术语和关键词
    const patterns = [
        /MBTI/i,
        /人格测试/i,
        /外向|内向/i,
        /浏览器自动化/i,
        /Page-Agent/i,
        /知识库/i,
        /RAG/i,
        /向量检索/i
    ]

    for (const pattern of patterns) {
        const match = task.match(pattern)
        if (match) {
            queries.push(match[0])
        }
    }

    return queries
}

/**
 * 增强任务描述：注入知识库内容
 * @param {string} task - 原始任务
 * @returns {Promise<string>} 增强后的任务
 */
async function enhanceTaskWithKnowledge(task) {
    // 提取查询关键词
    const queries = extractKnowledgeQueries(task)

    if (queries.length === 0) {
        return task
    }

    console.error(`[kb-enhance] Detected knowledge queries: ${queries.join(', ')}`)

    // 查询知识库
    const knowledgeParts = []
    for (const query of queries) {
        const knowledge = await queryKnowledgeBase(query)
        if (knowledge) {
            knowledgeParts.push(`【${query}】\n${knowledge}`)
        }
    }

    if (knowledgeParts.length === 0) {
        return task
    }

    // 注入知识库内容到任务描述
    const enhancedTask = `
${task}

---

【知识库参考】
以下是相关知识库内容，请参考这些信息完成任务：

${knowledgeParts.join('\n\n---\n\n')}
`

    console.error(`[kb-enhance] Enhanced task with ${knowledgeParts.length} knowledge items`)
    return enhancedTask
}

// ============================================================================

/**
 * 通知主 Agent 任务完成（照搬定时任务的 trigger_callback）
 */
async function notifyTaskComplete(result) {
    try {
        // 调用主 API 的通知接口（类似 trigger_callback 调用 /chat/sync）
        await fetch('http://localhost:9876/api/async-task/notify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'task_complete',
                result: result.success ? result.data : null,
                error: result.success ? null : result.data
            })
        })
        console.error(`[async-task] Task completed, notified main agent`)
    } catch (error) {
        console.error(`[async-task] Failed to notify completion: ${error.message}`)
    }
}

/**
 * 通知主 Agent 任务失败
 */
async function notifyTaskFailed(errorMessage) {
    try {
        await fetch('http://localhost:9876/api/async-task/notify', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                type: 'task_failed',
                error: errorMessage
            })
        })
        console.error(`[async-task] Task failed: ${errorMessage}`)
    } catch (error) {
        console.error(`[async-task] Failed to notify failure: ${error.message}`)
    }
}

// ============================================================================

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
        description: "Execute a browser automation task in background. Returns immediately. Will notify main agent when done. Examples: search web, fill forms, complete tests.",
        inputSchema: {
            task: z
                .string()
                .describe('Task description in natural language')
        },
    },
    async ({ task }) => {
        try {
            // 增强任务：注入知识库内容
            const enhancedTask = await enhanceTaskWithKnowledge(task)

            // 判断是否需要知识库增强的系统提示词
            const needsKnowledgeSystemPrompt = enhancedTask !== task

            // 构建配置（注入系统提示词）
            const config = {
                ...(Object.keys(llmConfig).length > 0 ? llmConfig : {}),
                systemInstruction: needsKnowledgeSystemPrompt
                    ? SYSTEM_PROMPTS.knowledge_enhanced
                    : undefined
            }

            // 异步执行（不等待）
            hub.executeTask(enhancedTask, config)
                .then(result => {
                    // 任务完成，通知主 API
                    notifyTaskComplete(result)
                })
                .catch(error => {
                    // 任务失败
                    notifyTaskFailed(error.message)
                })

            // 立即返回
            return {
                content: [
                    {
                        type: 'text',
                        text: JSON.stringify({
                            success: true,
                            message: 'Task started in background. Will notify when done.'
                        }, null, 2),
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
