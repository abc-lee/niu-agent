---
name: brain-region-management
description: Use when managing or configuring brain regions, adding new regions, or modifying region settings
---

# Brain Region Management

## Overview

**Brain regions should be configured in the preferences file, NOT manually created as entities in the knowledge graph.**

## When to Use

- Adding a new brain region
- Modifying brain region configuration
- Managing brain region settings

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
| **Remove a brain region** | Remove from preferences, restart |

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
| `description` | Yes | What the region stores |
| `priority` | Yes | `"core"` (always active) or `"category"` (on-demand) |
| `keywords` | No | List of Chinese keywords for heuristic entity-to-region matching |

The `keywords` field is used by `assign_entities_to_default_regions` to match entities to regions by name/description similarity. If omitted, the system falls back to a built-in keyword list for known default regions.

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
