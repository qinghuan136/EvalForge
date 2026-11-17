# EvalForge Trace接入与语义处理决策

## 决策

EvalForge仅提供**基础OpenTelemetry（OTel）Trace能力**，通过OTLP接收Span，不依赖或强制采用任何AI专用语义规范。

## 处理原则

1. **统一接收**：接收符合OTLP规范的Trace数据。用户可上报普通OTel Span，也可携带OpenInference、OTel GenAI或自定义属性。
2. **原样保留**：保留Span的基础字段、Attributes（属性及其值）和Events，不主动改写、转换或映射AI语义字段。
3. **通用展示**：基于Trace ID、Span ID、父子关系、时间、状态等基础信息，提供调用树、耗时瀑布图及原始属性查看。
4. **不做语义识别**：后端不按特定属性自动识别LLM、Agent、Tool、Retriever等类型，也不依赖专有字段生成Token、模型等专用统计。
5. **助手按需分析**：EvalForge助手可读取原始Trace数据进行推理分析，但结论以实际记录为依据；缺少的信息不推断为事实。

## 边界

**支持任意属性的存储与展示，不等于支持其专有语义。** 用户自行决定埋点方式及属性内容，EvalForge不要求使用OpenInference Instrumentation。

接收端配置和写入行为见 [OTLP Trace 上报](OTLP接收.md)。
