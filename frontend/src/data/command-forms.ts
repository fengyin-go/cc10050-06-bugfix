/** 领域命令的表单描述：每个命令需要采集的参数、占位提示与是否只读。 */
export type CommandField = {
  key: string
  label: string
  placeholder?: string
  /** 来源未确定时只读的字段（如签收单只能由样本采集归属带出）。 */
  readOnlyWhen?: (row: Record<string, unknown>) => boolean
  defaultValue?: (row: Record<string, unknown>) => string
}

export type CommandForm = {
  /** page 级命令不绑定具体记录；row 级命令作用于表格某一行。 */
  scope: 'row' | 'page'
  title: string
  fields: CommandField[]
  successHint: string
}

export const COMMAND_FORMS: Record<string, CommandForm> = {
  提交恢复: {
    scope: 'row',
    title: '提交恢复申请',
    fields: [
      { key: 'handler', label: '处理人', placeholder: '当前处理人工号/姓名' },
    ],
    successHint: '恢复凭据已开出，来源未确定时仅保留不计入产出',
  },
  补核来源: {
    scope: 'row',
    title: '补核样本采集来源',
    fields: [
      { key: 'source_site', label: '来源站点（须与样本采集归属一致）', placeholder: '如：南站' },
      {
        key: 'receipt_no',
        label: '来源签收单（来源未确定前只读，由采集归属带出）',
        placeholder: '补核通过后自动采信样本库签收单',
        readOnlyWhen: (row) => String(row['归属状态'] ?? '') !== '已确认',
      },
      { key: 'handler', label: '补核处理人', placeholder: '执行补核的处理人' },
    ],
    successHint: '采集归属核对通过后自动完成恢复并按来源记账',
  },
  恢复回执: {
    scope: 'row',
    title: '查看恢复回执',
    fields: [
      { key: 'handler', label: '查看人（转交后仅授权处理人可看）', placeholder: '处理人' },
    ],
    successHint: '回执显示本次是否已计入、来源签收单与实际记账站点',
  },
  并入别名: {
    scope: 'row',
    title: '站点别名并入区域组',
    fields: [
      { key: 'target_group', label: '并入目标组别', placeholder: '如：南区组' },
    ],
    successHint: '别名并入后，来源账目解析到组账目站点',
  },
  归属转交: {
    scope: 'row',
    title: '站点归属转交',
    fields: [
      { key: 'target_site', label: '目标站点', placeholder: '转交后记账的可追溯站点' },
      { key: 'handler', label: '处理人', placeholder: '获得单次恢复依据查看授权的处理人' },
    ],
    successHint: '转交留痕，原处理人仅可查看被授权的单次恢复依据',
  },
  解析记账站点: {
    scope: 'row',
    title: '解析当前记账站点',
    fields: [],
    successHint: '输出来源站点当前对应的可追溯记账站点',
  },
  恢复产出明细: {
    scope: 'page',
    title: '按恢复凭据核对产出归属',
    fields: [],
    successHint: '读取每笔恢复的归属与凭据，按来源归属核对产出指标',
  },
}
