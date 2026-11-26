<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button
          v-for="command in pageCommands"
          :key="command"
          class="btn"
          :data-testid="`page-command-${command}`"
          type="button"
          @click="openCommand(command)"
        >
          {{ command }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button
              v-for="command in rowCommands"
              :key="command"
              class="link command-link"
              type="button"
              :data-testid="`command-${row.id}-${command}`"
              @click="openCommand(command, row)"
            >
              {{ command }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <div v-if="commandForm" class="modal-backdrop" data-testid="command-modal">
      <form class="modal" @submit.prevent="submitCommand">
        <h3>{{ commandForm.title }}</h3>
        <p v-if="commandHint" class="command-hint">{{ commandHint }}</p>
        <label v-for="field in commandFields" :key="field.key" class="create-field">
          <span>{{ field.label }}</span>
          <input
            v-model="commandValues[field.key]"
            :data-testid="`command-input-${field.key}`"
            :placeholder="field.placeholder"
            :readonly="isReadOnly(field)"
            :class="{ readonly: isReadOnly(field) }"
          />
        </label>
        <pre v-if="commandResult" class="command-result" :data-testid="commandResult.ok ? 'command-ok' : 'command-fail'">{{ formatResult(commandResult) }}</pre>
        <div class="page-actions">
          <button class="btn" type="button" @click="closeCommand">关闭</button>
          <button class="btn primary" data-testid="submit-command" type="submit" :disabled="submitting">
            {{ submitting ? '提交中…' : '确认执行' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
  runCommand as applyCommand,
} from '@/api/backend-service'
import { COMMAND_FORMS, type CommandField } from '@/data/command-forms'
import type { CommandResult, EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const activeCommand = ref('')
const activeRow = ref<EntryRow | null>(null)
const commandValues = ref<Record<string, string>>({})
const submitting = ref(false)
const commandResult = ref<CommandResult | null>(null)

const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

const rowCommands = computed(() =>
  (meta.commands ?? []).filter((name) => COMMAND_FORMS[name]?.scope === 'row'),
)
const pageCommands = computed(() =>
  (meta.commands ?? []).filter((name) => COMMAND_FORMS[name]?.scope === 'page'),
)
const commandForm = computed(() => (activeCommand.value ? COMMAND_FORMS[activeCommand.value] : null))
const commandFields = computed<CommandField[]>(() => commandForm.value?.fields ?? [])
const commandHint = computed(() => commandForm.value?.successHint ?? '')

function isReadOnly(field: CommandField): boolean {
  if (!field.readOnlyWhen || !activeRow.value) return false
  return field.readOnlyWhen(activeRow.value as unknown as Record<string, unknown>)
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function openCommand(command: string, row?: EntryRow) {
  activeCommand.value = command
  activeRow.value = row ?? null
  commandResult.value = null
  const form = COMMAND_FORMS[command]
  commandValues.value = Object.fromEntries(
    form.fields.map((field) => [
      field.key,
      row && field.defaultValue ? field.defaultValue(row as unknown as Record<string, unknown>) : '',
    ]),
  )
}

function closeCommand() {
  activeCommand.value = ''
  activeRow.value = null
  commandResult.value = null
  commandValues.value = {}
}

async function submitCommand() {
  submitting.value = true
  errorMessage.value = ''
  try {
    const entryId = activeRow.value ? Number(activeRow.value.id) : undefined
    commandResult.value = await applyCommand(meta.key, activeCommand.value, commandValues.value, entryId)
    await reload()
  } catch (error) {
    commandResult.value = {
      ok: false,
      message: error instanceof Error ? error.message : '命令执行失败',
    }
  } finally {
    submitting.value = false
  }
}

function formatResult(result: CommandResult): string {
  return JSON.stringify(result.data ?? result, null, 2)
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.command-link {
  color: #1d6fbf;
}
.command-hint {
  margin: 0 0 8px;
  color: #666;
  font-size: 13px;
}
.command-result {
  max-height: 260px;
  overflow: auto;
  background: #f6f8fa;
  border: 1px solid #e1e4e8;
  border-radius: 6px;
  padding: 10px;
  font-size: 12px;
  text-align: left;
  white-space: pre-wrap;
  word-break: break-all;
}
.command-result:not([data-testid='command-ok']) {
  border-color: #e0a3a3;
  background: #fdf3f3;
}
.readonly {
  background: #f0f0f0;
  color: #777;
}
</style>
