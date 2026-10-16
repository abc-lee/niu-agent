---
name: brain-region-management
description: Use when managing or configuring brain regions, adding new regions, modifying region settings, or deciding whether to adjust brain region states
status: active
created: 2026-06-16
last_tested: 2026-06-16
---

# Brain Region Management

## Overview

**Brain regions should be configured in the preferences file, NOT manually created as entities in the knowledge graph.**
**Brain region states (lit/dimmed) are runtime behavior: 点亮/熄灭由对话上下文自动触发，平时不主动总结状态、不主动调整，仅在专业任务中感到注入信息不聚焦时按需调整。**

## Runtime Behavior

- 脑区的点亮/熄灭是对话上下文自动触发的，**不要轻易总结哪些脑区点亮/熄灭**——提到脑区名字本身就会触发它点亮，等于主动干扰自然状态
- **闲聊时**：脑区自然点亮/熄灭，不用管
- **专业任务时**：注入信息总量有限，点亮的脑区越多每个脑区注入量越少——感到干扰多、注入不准时，点亮需要的、熄灭无关的，让知识聚焦
- **调脑区是"事后纠偏"手段，不是"事前准备"步骤**——平时不碰，干活时感觉不对再调
- **职责分工**：短对话（用户主动说话）由用户盯着并手动调整；长程任务（多轮工具调用）用户不关注，由助手自己优化
- 不主动用 brain_region_dim/activate 工具，除非用户明确要求或在长任务中确实需要聚焦

## When to Use

- Adding a new brain region
- Modifying brain region configuration
- Managing brain region settings
- Deciding whether to adjust brain region states during tasks

## Core Principle

```
❌ BAD: Manually create brain region entities in the knowledge graph
✅ GOOD: Configure brain regions in ~/.niu/preferences.json, then restart
```

## Quick Reference

| Action | How to Do It |
|--------|--------------|
| **Add a new brain region** | Edit `~/.niu/preferences.json`, add to `brain_regions.defaults` |
| **Apply changes** | Restart the system after modifying preferences |
| **Remove a brain region** | Configured-out regions are cleaned up automatically by the dissolve mechanism (member count < 100 threshold + 3 consecutive sync rounds + orphan protection — takes several sync rounds to apply; ghosts are retained due to orphan protection) |

## Implementation

### Step 1: Edit Preferences File

```bash
# Open the preferences file
~/.niu/preferences.json
```

### Step 2: Add Brain Region to Defaults

Add your new brain region to the `brain_regions.defaults` section. Each entry supports:

| Field | Required | Description |
|-------|----------|-------------|
| `label` | Yes | Region display name (e.g., "工作事务") |
| `description` | Yes | What the region stores (only takes effect when the region is created — existing regions' descriptions are maintained by the knowledge graph; changing `description` in config does not affect existing regions) |
| `priority` | Yes | `"permanent"` (360天半衰期，永不删除) / `"long"` (360天) / `"medium"` (180天) / `"short"` (90天) |
| `keywords` | No | Used for runtime classification — when a hit entity's name contains any keyword, it is classified into this region (first config-order match wins); keywords all miss → fall back to entity_type mapping |

Region membership edges (`包含` belongs-to) are created naturally by LLM-prompt-guided knowledge graph operations — dream-evolver injects the brain region list and guides edge creation, entity extraction / document ingestion prompts do the same, and skill sync links skill entities to the knowledge system region.

### Step 3: Restart

Restart the system - it will automatically create the brain region and establish proper connections.

## Common Mistakes

### ❌ Mistake 1: Manually Creating Brain Region Entities

**What goes wrong:**
- Creates orphaned entities
- Doesn't establish proper connections
- Can't be managed through the system
- Leads to confusion and cleanup work

**Fix:** Delete any manually created brain region entities, configure in preferences instead.

### ❌ Mistake 2: Forgetting to Restart

**What goes wrong:** Changes don't take effect.

**Fix:** Always restart after modifying brain region configuration.

## Real-World Example

**Scenario:** Want to add an "Organizations" brain region to manage company entities.

**❌ Wrong approach:**
```
Create entity "Organizations脑区" in knowledge graph
Manually try to connect entities
```

**✅ Right approach:**
1. Edit `~/.niu/preferences.json`, add "Organizations" to brain regions
2. Restart system
3. System automatically creates the region and connects relevant entities

## Why This Matters

- Brain regions are system-managed, not user-created entities
- Configuration in preferences ensures proper setup and connections
- Prevents orphaned entities and cleanup work
- Makes brain regions persistent and manageable

<!-- 执行提醒 -->
<!-- 此区域用于重申已有规则，不引入新规则。规则没错但没被遵守时在这里添加提醒。 -->
