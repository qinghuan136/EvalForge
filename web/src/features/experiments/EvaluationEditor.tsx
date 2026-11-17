import type { EvaluatorConfig, EvaluatorDefinition } from './api'

type Source = 'default' | 'path' | 'literal'
type Parameter = { source: Source; value: string }
export type EvaluationDraft = { id: number; name: string; rule: string; parameters: Record<string, Parameter> }

const rules: Record<string, string> = {
  contains: '包含文本', exact_match: '完全匹配', regex: '正则匹配',
  levenshtein_distance: '编辑距离', json_distance: 'JSON 差异',
}
const labels: Record<string, string> = { actual: '实际值', expected: '预期值', case_sensitive: '区分大小写' }

function parameter(draft: EvaluationDraft, definition: EvaluatorDefinition, name: string): Parameter {
  return draft.parameters[name] ?? { source: definition.input_schema.required?.includes(name) ? 'path' : 'default', value: '' }
}

export function evaluationConfigs(drafts: EvaluationDraft[], definitions: EvaluatorDefinition[]): EvaluatorConfig[] {
  const names = new Set<string>()
  return drafts.map((draft, index) => {
    const name = draft.name.trim()
    if (!name) throw new Error(`请填写评分项 ${index + 1} 的名称。`)
    if (names.has(name)) throw new Error(`评分项名称「${name}」重复。`)
    names.add(name)
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
    return { name, evaluator_key: definition.key, input_mapping: { path_mapping, literal_mapping } }
  })
}

export function EvaluationEditor({ drafts, definitions, onAdd, onChange, onRemove, disabled }: {
  drafts: EvaluationDraft[]
  definitions: EvaluatorDefinition[]
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
        const definition = definitions.find((item) => item.key === draft.rule)
        return (
          <fieldset className="evaluation-item" key={draft.id}>
            <legend>评分项 {index + 1}</legend>
            <div className="evaluation-item-header">
              <label className="field">评分项名称<input value={draft.name} maxLength={100} required
                placeholder="例如：答案匹配" onChange={(event) => onChange({ ...draft, name: event.target.value })} /></label>
              <button className="button" type="button" onClick={() => onRemove(draft.id)} aria-label={`移除评分项 ${index + 1}`}>移除</button>
            </div>
            <label className="field">评分器<select aria-label="评分器" required value={draft.rule} onChange={(event) =>
              onChange({ ...draft, rule: event.target.value, parameters: {} })}>
              <option value="">请选择评分器</option>
              {definitions.map((item) => <option key={item.key} value={item.key}>{rules[item.key] ?? item.key}</option>)}
            </select></label>
            {definition && <p className="muted"><code>$.output</code> 是 Agent 响应，<code>$.reference</code> 是数据集参考输出。</p>}
            {definition && Object.entries(definition.input_schema.properties).map(([key, schema]) => {
              const { source, value } = parameter(draft, definition, key)
              const required = definition.input_schema.required?.includes(key)
              const label = labels[key] ? `${labels[key]}（${key}）` : key
              const placeholder = key === 'expected' ? '$.reference.answer' : '$.output.answer'
              function update(next: Parameter) {
                onChange({ ...draft, parameters: { ...draft.parameters, [key]: next } })
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
      <div><button className="button" type="button" onClick={onAdd} disabled={!definitions.length || drafts.length >= 100}>添加评分器</button></div>
    </fieldset>
  )
}
