import { apiRequest } from '../../lib/api'
import { importDataset } from '../datasets/api'
import { cancelExperiment, createExperiment } from '../experiments/api'
import type { CreateExperiment } from '../experiments/api'
import { createRunner, deleteRunner } from '../runners/api'
import type { RunnerCreated } from '../runners/api'
import type { PageContext } from './pageContext'

export type AssistantSession = { id: number; title: string; version: number }
export type AssistantMessage = {
  id: number
  role: 'user' | 'assistant'
  text: string
  model_name: string | null
  source_experiment_id: number | null
  page_context?: PageContext | null
}
export type Transcript = AssistantSession & { messages: AssistantMessage[] }

type DatasetImport = {
  name: string; format: 'csv' | 'jsonl'; content: string; description: string | null
  input_keys: string[]; output_keys: string[]; metadata_keys: string[]
}

export type AssistantAction = { id: string } & (
  { tool: 'RUNNER_create_runner'; arguments: { name: string } } |
  { tool: 'RUNNER_delete_runner'; arguments: { runner_id: string } } |
  { tool: 'DATASET_import_dataset'; arguments: DatasetImport } |
  { tool: 'EXPERIMENT_create_experiment'; arguments: CreateExperiment & { dataset_id: number } } |
  { tool: 'EXPERIMENT_cancel_experiment'; arguments: { experiment_id: number } }
)

export type AssistantActionResult = { message: string; runner?: RunnerCreated }

const sessions = '/assistant/sessions'

export function getAssistantSessions(signal: AbortSignal) {
  return apiRequest<{ items: AssistantSession[]; models: string[] }>(sessions, { signal })
}

export function createAssistantSession(title: string) {
  return apiRequest<AssistantSession>(sessions, { method: 'POST', json: { title } })
}

export function getAssistantSession(id: number, signal: AbortSignal) {
  return apiRequest<Transcript>(`${sessions}/${id}`, { signal })
}

export function sendAssistantMessage(id: number, expected_version: number, message: string,
  model_name: string, page_context?: PageContext) {
  return apiRequest<{ version: number; messages: AssistantMessage[]; actions?: AssistantAction[] }>(`${sessions}/${id}/chat`, {
    method: 'POST', json: { page_context, expected_version, message, model_name },
  })
}

export async function executeAssistantAction(action: AssistantAction): Promise<AssistantActionResult> {
  switch (action.tool) {
    case 'RUNNER_create_runner': {
      const runner = await createRunner(action.arguments.name)
      return { message: `Runner 已创建（${runner.runner.runner_id}）`, runner }
    }
    case 'RUNNER_delete_runner':
      await deleteRunner(action.arguments.runner_id)
      return { message: 'Runner 已删除，连接凭证已撤销。' }
    case 'DATASET_import_dataset': {
      const { name, description, format, content, input_keys, output_keys, metadata_keys } = action.arguments
      const body = new FormData()
      body.set('name', name)
      body.set('format', format)
      body.set('file', new Blob([content], { type: 'text/plain;charset=utf-8' }), `dataset.${format}`)
      if (description !== null) body.set('description', description)
      input_keys.forEach((key) => body.append('input_keys', key))
      output_keys.forEach((key) => body.append('output_keys', key))
      metadata_keys.forEach((key) => body.append('metadata_keys', key))
      const result = await importDataset(body)
      return { message: `已导入数据集 #${result.dataset_id}，版本 #${result.dataset_version_id}，共 ${result.example_count} 条样例。` }
    }
    case 'EXPERIMENT_create_experiment': {
      const { dataset_id, ...config } = action.arguments
      const experiment = await createExperiment(dataset_id, config)
      return { message: `已创建实验 #${experiment.experiment_id}，共 ${experiment.progress.job_count} 个 Job。` }
    }
    case 'EXPERIMENT_cancel_experiment': {
      const experiment = await cancelExperiment(action.arguments.experiment_id)
      const { experiment_id, status } = experiment
      if (status === 'cancelled') return { message: `实验 #${experiment_id} 已取消。` }
      if (status === 'cancelling') return { message: `实验 #${experiment_id} 的取消请求已受理，仍有任务待结束。` }
      return { message: `实验 #${experiment_id} 当前状态为 ${status}。` }
    }
  }
}
