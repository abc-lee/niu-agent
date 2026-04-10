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
