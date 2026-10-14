export type PatchSectionName =
  | 'Input Channels'
  | 'Aux Outputs'
  | 'Group Outputs'
  | 'Matrix Inputs'
  | 'Matrix Outputs'
  | 'Control Groups'

export interface PatchRow {
  id: string
  name: string
  inputs: string[]
  mergeInputs: string[]
  insertA: string[]
  insertB: string[]
  outputs: string[]
  copyAudio: string[]
  members: string[]
}

export interface SourceRow {
  port: string
  number: number
  input: string
  channel: string
  copyAudio: string[]
  directOuts: string[]
}

export interface SessionStats {
  bytes: number
  sockets: number
  namedSockets: number
  copyRoutes: number
  directOutputs: number
}

export interface SessionProfile {
  console: string
  modelCode: string
  formatRevision: string
  catalogRecordLength: number
}

export interface SessionModel {
  fileName: string
  sessionName: string
  profile: SessionProfile
  ioPorts: Array<{ number: number; name: string }>
  sources: SourceRow[]
  sections: Record<PatchSectionName, PatchRow[]>
  stats: SessionStats
  warnings: string[]
}

export interface ParseRequest {
  type: 'parse'
  fileName: string
  buffer: ArrayBuffer
}

export type ParseWorkerResponse =
  | { type: 'success'; session: SessionModel }
  | { type: 'error'; message: string }
