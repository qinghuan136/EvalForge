import { useQuery } from '@tanstack/react-query'
import { privateKey } from '../../app/queryClient'
import { errorMessage } from '../../lib/api'
import type { CodeEvaluator, Version } from '../evaluators/api'
import { getAllPages } from './api'
import type { Evaluator, EvaluatorConfig, EvaluatorDefinition } from './api'

type Source = 'default' | 'path' | 'literal'
type Parameter = { source: Source; value: string }
type DraftFields = { id: number; name: string; description?: string | null }
type BuiltinDraft = DraftFields & { kind: 'builtin'; rule: string; parameters: Record<string, Parameter> }
type CodeDraft = DraftFields & {
  kind: 'code'
  evaluatorId: number
  versionId: number | null
  savedVersion?: { id: number; number: number; name: string }
}
export type EvaluationDraft = BuiltinDraft | CodeDraft

const rules: Record<string, string> = {
  contains: '包含文本', exact_match: '完全匹配', regex: '正则匹配',
  levenshtein_distance: '编辑距离', json_distance: 'JSON 差异',
}
const labels: Record<string, string> = { actual: '实际值', expected: '预期值', case_sensitive: '区分大小写' }

function parameter(draft: BuiltinDraft, definition: EvaluatorDefinition, name: string): Parameter {
  return draft.parameters[name] ?? { source: definition.input_schema.required?.includes(name) ? 'path' : 'default', value: '' }
}

export function evaluationConfigs(drafts: EvaluationDraft[], definitions: EvaluatorDefinition[],
                                  codeEvaluators: CodeEvaluator[]): EvaluatorConfig[] {
  const names = new Set<string>()
  return drafts.map((draft, index) => {
    const name = draft.name.trim()
    if (!name) throw new Error(`请填写评分项 ${index + 1} 的名称。`)
    if (names.has(name)) throw new Error(`评分项名称「${name}」重复。`)
    names.add(name)
    if (draft.kind === 'code') {
      if (!codeEvaluators.some((item) => item.id === draft.evaluatorId)) {
        throw new Error(`请移除「${name}」的已归档评分器，或选择其他评分器。`)
      }
      if (draft.versionId === null) throw new Error(`请为「${name}」选择发布版本。`)
      return { kind: 'CODE', name, description: draft.description, code_evaluator_version_id: draft.versionId }
    }
    const definition = definitions.find((item) => item.key === draft.rule)
    if (!definition) throw new Error(`请为「${name}」选择评分器。`)
    const path_mapping: Record<string, string> = {}
    const literal_mapping: Record<string, unknown> = {}
    for (const [key, schema] of Object.entries(definition.input_schema.properties)) {
      const { source, value } = parameter(draft, definition, key)
      if (source === 'path') {
        if (!value.trim()) throw new Error(`请填写「${name}」的 ${key} 路径。`)
        path_mapping[key] = value.trim()
      } else if (source === 'literal') {
        try {
          literal_mapping[key] = schema.type === 'string' ? value : JSON.parse(value)
        } catch {
          throw new Error(`「${name}」的 ${key} 固定值须为有效的 JSON。`)
        }
      }
    }
    return { name, description: draft.description, evaluator_key: definition.key, input_mapping: { path_mapping, literal_mapping } }
  })
}

export function evaluationDrafts(evaluators: Evaluator[], definitions: EvaluatorDefinition[]): EvaluationDraft[] {
  return evaluators.map((evaluator, id) => {
    if ('kind' in evaluator) {
      return {
        id, kind: 'code', name: evaluator.name, description: evaluator.description,
        evaluatorId: evaluator.code_evaluator_id, versionId: evaluator.code_evaluator_version_id,
        savedVersion: {
          id: evaluator.code_evaluator_version_id, number: evaluator.code_evaluator_version_number,
          name: evaluator.code_evaluator_name,
        },
      }
    }
    const schema = definitions.find((item) => item.key === evaluator.evaluator_key)?.input_schema.properties
    const parameters: Record<string, Parameter> = {}
    for (const [name, value] of Object.entries(evaluator.input_mapping.path_mapping)) {
      parameters[name] = { source: 'path', value }
    }
    for (const [name, value] of Object.entries(evaluator.input_mapping.literal_mapping)) {
      parameters[name] = { source: 'literal', value: schema?.[name]?.type === 'string' && typeof value === 'string' ? value : JSON.stringify(value) }
    }
    return { id, kind: 'builtin', name: evaluator.name, rule: evaluator.evaluator_key, parameters,
      description: evaluator.description }
  })
}

function CodeVersionSelect({ draft, userId, available, onChange }: {
  draft: CodeDraft; userId: number; available: boolean; onChange: (draft: CodeDraft) => void
}) {
  const versions = useQuery({
    queryKey: [...privateKey, userId, 'code-evaluator-versions', draft.evaluatorId, 'all'],
    queryFn: ({ signal }) => getAllPages<Version>(`/evaluators/code/${draft.evaluatorId}/versions`, signal),
    enabled: available,
  })
  const options = available ? versions.data ?? [] : []
  const saved = draft.savedVersion
  return <>
    <label className="field">发布版本<select value={draft.versionId ?? ''} required
      disabled={!available || !versions.data}
      onChange={(event) => onChange({ ...draft, versionId: Number(event.target.value) || null })}>
      <option value="">{available && versions.isPending ? '正在加载版本…' : '请选择发布版本'}</option>
      {saved && !options.some((version) => version.id === saved.id) && <option value={saved.id}>v{saved.number}</option>}
      {options.map((version) => <option key={version.id} value={version.id}>v{version.version_number}</option>)}
    </select></label>
    {available && versions.isError && <p className="error-message" role="alert">{errorMessage(versions.error)}{' '}
      <button className="button" type="button" onClick={() => void versions.refetch()}>重试</button></p>}
    {available ? <p className="muted">评分器接收完整的 input、reference、output 和 metadata。</p>
      : <p className="muted">此评分器已归档。原配置版本仍可运行。保存新版前，请更换或移除此评分项。</p>}
  </>
}

export function EvaluationEditor({ userId, drafts, definitions, codeEvaluators, onAdd, onChange, onRemove, disabled }: {
  userId: number
  drafts: EvaluationDraft[]
  definitions: EvaluatorDefinition[]
  codeEvaluators: CodeEvaluator[]
  onAdd: () => void
  onChange: (draft: EvaluationDraft) => void
  onRemove: (id: number) => void
  disabled: boolean
}) {
  return (
    <fieldset className="evaluation-options" disabled={disabled}>
      <legend>评分配置（选填）</legend>
      {!drafts.length && <p className="muted">不添加评分器时，只执行 Agent 并记录结果。</p>}
      {drafts.map((draft, index) => {
        const builtin = draft.kind === 'builtin' ? draft : null
        const definition = definitions.find((item) => item.key === builtin?.rule)
        const available = draft.kind === 'code' && codeEvaluators.some((item) => item.id === draft.evaluatorId)
        const selection = draft.kind === 'code' ? `code:${draft.evaluatorId}` : draft.rule
        return (
          <fieldset className="evaluation-item" key={draft.id}>
            <legend>评分项 {index + 1}</legend>
            <div className="evaluation-item-header">
              <label className="field">评分项名称<input value={draft.name} maxLength={100} required
                placeholder="例如：答案匹配" onChange={(event) => onChange({ ...draft, name: event.target.value })} /></label>
              <button className="button" type="button" onClick={() => onRemove(draft.id)} aria-label={`移除评分项 ${index + 1}`}>移除</button>
            </div>
            <label className="field">评分器<select aria-label="评分器" required value={selection} onChange={(event) => {
              const value = event.target.value
              const fields = { id: draft.id, name: draft.name, description: draft.description }
              onChange(value.startsWith('code:')
                ? { ...fields, kind: 'code', evaluatorId: Number(value.slice(5)), versionId: null }
                : { ...fields, kind: 'builtin', rule: value, parameters: {} })
            }}>
              <option value="">请选择评分器</option>
              {draft.kind === 'code' && !available && <option value={selection}>
                {draft.savedVersion?.name ?? '自定义评分器'}（已归档）
              </option>}
              <optgroup label="系统评分器">
                {definitions.map((item) => <option key={item.key} value={item.key}>{rules[item.key] ?? item.key}</option>)}
              </optgroup>
              {codeEvaluators.length > 0 && <optgroup label="自定义评分器">
                {codeEvaluators.map((item) => <option key={item.id} value={`code:${item.id}`}>{item.name}</option>)}
              </optgroup>}
            </select></label>
            {draft.kind === 'code' && <CodeVersionSelect draft={draft} userId={userId} available={available} onChange={onChange} />}
            {definition && <p className="muted"><code>$.output</code> 是 Agent 响应，<code>$.reference</code> 是数据集参考输出。</p>}
            {builtin && definition && Object.entries(definition.input_schema.properties).map(([key, schema]) => {
              const { source, value } = parameter(builtin, definition, key)
              const required = definition.input_schema.required?.includes(key)
              const label = labels[key] ? `${labels[key]}（${key}）` : key
              const placeholder = key === 'expected' ? '$.reference.answer' : '$.output.answer'
              const update = (next: Parameter) => {
                onChange({ ...builtin, parameters: { ...builtin.parameters, [key]: next } })
              }
              return (
                <div className="evaluation-parameter" key={key}>
                  <label className="field">{label}的来源<select value={source} onChange={(event) =>
                    update({ source: event.target.value as Source, value: '' })}>
                    {!required && <option value="default">使用默认值{schema.default !== undefined ? `（${String(schema.default)}）` : ''}</option>}
                    <option value="path">字段路径</option>
                    <option value="literal">固定值</option>
                  </select></label>
                  {source === 'path' && <label className="field">{label}路径<input required maxLength={1024}
                    placeholder={placeholder} value={value} onChange={(event) => update({ source, value: event.target.value })} /></label>}
                  {source === 'literal' && <label className="field">{label}固定值<input required={schema.type !== 'string'}
                    placeholder={schema.type === 'string' ? '输入文本' : '输入 JSON 值'} value={value}
                    onChange={(event) => update({ source, value: event.target.value })} /></label>}
                </div>
              )
            })}
          </fieldset>
        )
      })}
      <div><button className="button" type="button" onClick={onAdd}
        disabled={(!definitions.length && !codeEvaluators.length) || drafts.length >= 100}>添加评分器</button></div>
    </fieldset>
  )
}
