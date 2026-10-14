import type {
  PatchRow,
  PatchSectionName,
  SessionProfile,
  SessionModel,
  SourceRow,
} from '../types'

const textDecoder = new TextDecoder('windows-1252')
const DA_RECORD = [0xda, 0x00, 0x3a, 0x00]
const CB_RECORD_PREFIX = [0xcb, 0x00]
const CA_RECORD = [0xca, 0x00, 0x6c]
const INPUT_ASSIGN_SUFFIX = [0x00, 0x01, 0x01, 0x00, 0x00, 0x00]
const SOCKET_LINK_MARKER = [0xe2, 0x12, 0x00, 0x00, 0x00, 0x00]
const SOCKET_NAME_FIELD = 32
const SOCKET_TRAILER = [0x01, 0xff, 0xff, 0xff, 0xff, 0x40, 0x0e]

type SocketMap = Map<number, Map<number, string>>
type Routes = Map<string, string[]>

interface CatalogEntry {
  index: number
  position: number
  name: string
  stereo: boolean
}

const consoleModels: Record<string, string> = {
  Q1: 'Q12',
  Q2: 'Q225',
  Q3: 'Q338',
  Q5: 'Q5',
  SDQ: 'Q7',
  Q8: 'Q852',
  SD5: 'SD5',
  SD7: 'SD7',
  SD8: 'SD10',
  SD9: 'SD11',
  SDC: 'SD12',
}
const catalogRecordLengths = [0x50, 0x58, 0x78, 0x79]

const sectionTypes: Array<[number, PatchSectionName, string]> = [
  [0x01, 'Input Channels', ''],
  [0x05, 'Aux Outputs', 'A'],
  [0x06, 'Group Outputs', 'G'],
  [0x0f, 'Matrix Inputs', 'Mi'],
  [0x10, 'Matrix Outputs', 'M'],
  [0x0a, 'Control Groups', 'CG'],
]

function matches(data: Uint8Array, offset: number, pattern: number[]): boolean {
  if (offset < 0 || offset + pattern.length > data.length) return false
  for (let i = 0; i < pattern.length; i += 1) {
    if (data[offset + i] !== pattern[i]) return false
  }
  return true
}

function* findAll(
  data: Uint8Array,
  pattern: number[],
  start = 0,
  end = data.length,
): Generator<number> {
  let offset = start
  while (offset < end) {
    const found = data.indexOf(pattern[0], offset)
    if (found < 0 || found + pattern.length > end) return
    if (matches(data, found, pattern)) yield found
    offset = found + 1
  }
}

function* findCatalogRecords(
  data: Uint8Array,
  recordLength: number,
): Generator<number> {
  for (const offset of findAll(data, CB_RECORD_PREFIX)) {
    if (data[offset + 2] === recordLength) yield offset
  }
}

function parseProfile(data: Uint8Array): SessionProfile {
  if (ascii(data, 0, 11) !== 'DiGiCo') {
    throw new Error('This file does not look like a DiGiCo session.')
  }
  const modelCode = ascii(data, 11, 4)
  const formatRevision = ascii(data, 20, 5)
  const candidates = catalogRecordLengths.map((recordLength) => {
    let channels = 0
    let sockets = 0
    for (const offset of findCatalogRecords(data, recordLength)) {
      if (offset + 9 >= data.length) continue
      const type = data[offset + 5]
      const nameLength = data[offset + 8]
      if (nameLength > 40 || offset + 9 + nameLength > data.length) continue
      const name = data.subarray(offset + 9, offset + 9 + nameLength)
      if (!isPrintable(name)) continue
      if (type === 0x01) channels += 1
      if (isAudioSocketRecord(data, offset)) sockets += 1
    }
    return { recordLength, channels, sockets }
  })
  const layout = candidates
    .filter(({ channels, sockets }) => channels > 0 && sockets > 0)
    .sort((a, b) => b.channels + b.sockets - (a.channels + a.sockets))[0]
  if (!layout) {
    throw new Error(
      `The DiGiCo session header was recognized (${modelCode || 'unknown'}/${formatRevision || 'unknown'}), but its catalog record layout is not supported.`,
    )
  }
  return {
    console: consoleModels[modelCode] || modelCode || 'Unknown DiGiCo',
    modelCode,
    formatRevision,
    catalogRecordLength: layout.recordLength,
  }
}

function lastPatternBefore(
  data: Uint8Array,
  pattern: number[],
  start: number,
  end: number,
): number {
  let last = -1
  for (const offset of findAll(data, pattern, start, end)) last = offset
  return last
}

function isPrintable(bytes: Uint8Array): boolean {
  // DiGiCo names are Windows-1252, not strictly 7-bit ASCII. Real sessions
  // can contain bytes above 0x7f (for example a mistyped or accented label).
  return bytes.every((value) => value >= 32 && value !== 127)
}

function ascii(data: Uint8Array, start: number, length: number): string {
  return textDecoder.decode(data.subarray(start, start + length)).trim()
}

function naturalCompare(a: string, b: string): number {
  return a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' })
}

function socketPrefix(socket: string): string {
  return socket.includes(':') ? socket.split(':', 1)[0] : socket
}

function socketKind(socket: string): string {
  const suffix = socket.includes(':') ? socket.slice(socket.indexOf(':') + 1) : socket
  return suffix.replace(/\s*\d+$/, '').trim()
}

function isAudioIoType(type: number): boolean {
  return type >= 0x1c && type <= 0x5d
}

function isAudioSocketRecord(data: Uint8Array, offset: number): boolean {
  return (
    isAudioIoType(data[offset + 5]) &&
    matches(data, offset + 9 + SOCKET_NAME_FIELD, SOCKET_TRAILER)
  )
}

function resolveSocket(
  sockets: SocketMap,
  type: number,
  index: number,
): string | undefined {
  return sockets.get(type)?.get(index)
}

function buildSocketMap(data: Uint8Array, recordLength: number): SocketMap {
  const result: SocketMap = new Map()
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 49 >= data.length) continue
    const index = data[offset + 4]
    const type = data[offset + 5]
    const length = data[offset + 8]
    if (length < 2 || length > 40 || offset + 9 + length > data.length) continue
    if (!isAudioSocketRecord(data, offset)) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(bytes)) continue
    const byIndex = result.get(type) ?? new Map<number, string>()
    if (!byIndex.has(index)) byIndex.set(index, ascii(data, offset + 9, length))
    result.set(type, byIndex)
  }
  return result
}

function buildPortNames(data: Uint8Array): Map<number, string> {
  const result = new Map<number, string>()
  for (const offset of findAll(data, CA_RECORD)) {
    if (offset + 6 >= data.length) continue
    const type = data[offset + 4]
    const length = data[offset + 5]
    if (length < 1 || length > 40 || offset + 6 + length > data.length) continue
    const bytes = data.subarray(offset + 6, offset + 6 + length)
    if (!isPrintable(bytes) || result.has(type)) continue
    result.set(type, ascii(data, offset + 6, length))
  }
  return result
}

function buildCatalog(
  data: Uint8Array,
  recordLength: number,
): Map<number, CatalogEntry[]> {
  const wantedTypes = new Set(sectionTypes.map(([type]) => type))
  const byType = new Map<number, Map<number, CatalogEntry>>()
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue
    const index = data[offset + 4]
    const type = data[offset + 5]
    const position = data[offset + 6]
    const length = data[offset + 8]
    if (!wantedTypes.has(type) || length > 32) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(bytes)) continue
    const entries = byType.get(type) ?? new Map<number, CatalogEntry>()
    entries.set(position, {
      index,
      position,
      name: ascii(data, offset + 9, length),
      stereo: data[offset + 41] === 2,
    })
    byType.set(type, entries)
  }

  const result = new Map<number, CatalogEntry[]>()
  for (const [type, entries] of byType) {
    result.set(type, [...entries.values()].sort((a, b) => a.position - b.position))
  }
  return result
}

function buildReferenceNames(
  data: Uint8Array,
  recordLength: number,
): Map<number, Map<number, string>> {
  const result = new Map<number, Map<number, string>>()
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue
    const index = data[offset + 4]
    const type = data[offset + 5]
    const length = data[offset + 8]
    if (length < 1 || length > 32) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(bytes)) continue
    const byIndex = result.get(type) ?? new Map<number, string>()
    if (!byIndex.has(index)) byIndex.set(index, ascii(data, offset + 9, length))
    result.set(type, byIndex)
  }
  return result
}

function buildStereoNames(data: Uint8Array, recordLength: number): Set<string> {
  const result = new Set<string>()
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue
    const length = data[offset + 8]
    if (length < 1 || length > 32 || data[offset + 41] !== 2) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (isPrintable(bytes) && !bytes.includes(0x3a)) {
      result.add(ascii(data, offset + 9, length))
    }
  }
  return result
}

function nextSocket(sockets: SocketMap, socket: string): string | undefined {
  for (const byIndex of sockets.values()) {
    for (const [index, name] of byIndex) {
      if (name === socket) return byIndex.get(index + 1)
    }
  }
  return undefined
}

function buildInputAssignments(
  data: Uint8Array,
  sockets: SocketMap,
  references: Map<number, Map<number, string>>,
  stereoNames: Set<string>,
): {
  socketToChannel: Map<string, string>
  channelToInputs: Map<string, string[]>
  channelInputOccurrences: Map<string, string[][]>
  fallbackInputs: Map<string, string[]>
} {
  const socketNames = new Set(
    [...sockets.values()].flatMap((byIndex) => [...byIndex.values()]),
  )
  const votes = new Map<string, Map<string, number>>()
  const physicalVotes = new Map<string, Map<string, number>>()
  const latestInputs = new Map<string, string[]>()
  const inputOccurrences = new Map<string, string[][]>()

  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 300 >= data.length || data[offset + 4] !== 0x01) continue
    const length = data[offset + 8]
    if (length > 40) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(bytes)) continue
    const name = ascii(data, offset + 9, length)
    const regionEnd = Math.min(data.length, offset + 300)
    let recordInputs: string[] = []
    for (const marker of findAll(data, INPUT_ASSIGN_SUFFIX, offset, regionEnd)) {
      if (marker < offset + 2) continue
      const index = data[marker - 2]
      const type = data[marker - 1]
      let source =
        resolveSocket(sockets, type, index) ?? references.get(type)?.get(index)
      if (!source) continue
      const leg = name.match(/ ([LR])$/)?.[1]
      if (
        leg &&
        !socketNames.has(source) &&
        !source.endsWith(` ${leg}`)
      ) {
        source = `${source} ${leg}`
      } else if (
        stereoNames.has(name) &&
        (stereoNames.has(source) || type === 0x0e) &&
        !source.endsWith(' L')
      ) {
        source = `${source} L`
      }
      const bySocket = votes.get(name) ?? new Map<string, number>()
      bySocket.set(source, (bySocket.get(source) ?? 0) + 1)
      const secondaryIndex = data[marker + INPUT_ASSIGN_SUFFIX.length]
      const secondaryType = data[marker + INPUT_ASSIGN_SUFFIX.length + 1]
      const secondary =
        resolveSocket(sockets, secondaryType, secondaryIndex) ??
        references.get(secondaryType)?.get(secondaryIndex)
      if (secondary && secondary !== source) {
        bySocket.set(secondary, (bySocket.get(secondary) ?? 0) + 1)
      }
      votes.set(name, bySocket)
      recordInputs =
        secondary && secondary !== source ? [source, secondary] : [source]
      latestInputs.set(name, recordInputs)
      if (socketNames.has(source)) {
        const physical = physicalVotes.get(name) ?? new Map<string, number>()
        physical.set(source, (physical.get(source) ?? 0) + 1)
        if (secondary && socketNames.has(secondary)) {
          physical.set(secondary, (physical.get(secondary) ?? 0) + 1)
        }
        physicalVotes.set(name, physical)
      }
      break
    }
    if (!recordInputs.length) latestInputs.set(name, [])
    const occurrences = inputOccurrences.get(name) ?? []
    occurrences.push(recordInputs)
    inputOccurrences.set(name, occurrences)
  }

  const socketClaims = new Map<string, { name: string; votes: number }>()
  const channelToInputs = latestInputs
  const fallbackInputs = new Map(
    [...votes].map(([name, bySocket]) => [
      name,
      [...bySocket]
        .sort((a, b) => b[1] - a[1])
        .map(([source]) => source),
    ]),
  )

  for (const [name, bySocket] of physicalVotes) {
    const ranked = [...bySocket].sort((a, b) => b[1] - a[1])
    const [source, count] = ranked[0]
    const claim = (candidate: string) => {
      const previous = socketClaims.get(candidate)
      if (!previous || count > previous.votes) {
        socketClaims.set(candidate, { name, votes: count })
      }
    }
    claim(source)
    if (stereoNames.has(name)) {
      const right = nextSocket(sockets, source)
      if (right) {
        claim(right)
      }
    }
  }

  return {
    socketToChannel: new Map(
      [...socketClaims].map(([socket, claim]) => [socket, claim.name]),
    ),
    channelToInputs,
    channelInputOccurrences: inputOccurrences,
    fallbackInputs,
  }
}

function buildInserts(
  data: Uint8Array,
  sockets: SocketMap,
  references: Map<number, Map<number, string>>,
): Map<string, { insertA: string[]; insertB: string[] }> {
  const latest = new Map<string, { insertA: string[]; insertB: string[] }>()
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 206 >= data.length || data[offset + 4] !== 0x08) continue
    const length = data[offset + 8]
    if (length < 1 || length > 40) continue
    const nameBytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(nameBytes)) continue
    const readValues = (pairOffsets: number[]) => {
      const values: string[] = []
      for (const pairOffset of pairOffsets) {
        const index = data[pairOffset]
        const type = data[pairOffset + 1]
        const value =
          resolveSocket(sockets, type, index) ?? references.get(type)?.get(index)
        if (value && !values.includes(value)) values.push(value)
      }
      return values
    }
    latest.set(ascii(data, offset + 9, length), {
      insertA: readValues([offset + 124, offset + 132]),
      insertB: readValues([offset + 196, offset + 204]),
    })
  }
  return latest
}

function buildMergeInputs(
  data: Uint8Array,
  sockets: SocketMap,
  references: Map<number, Map<number, string>>,
  stereoNames: Set<string>,
): Map<string, string[]> {
  const latest = new Map<string, string[]>()
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 102 >= data.length || data[offset + 4] !== 0x0b) continue
    const length = data[offset + 8]
    if (length < 1 || length > 40) continue
    const nameBytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(nameBytes)) continue
    const name = ascii(data, offset + 9, length)
    const index = data[offset + 100]
    const type = data[offset + 101]
    let source =
      resolveSocket(sockets, type, index) ?? references.get(type)?.get(index)
    if (
      source &&
      stereoNames.has(name) &&
      stereoNames.has(source) &&
      !source.endsWith(' L')
    ) {
      source = `${source} L`
    }
    latest.set(name, source ? [source] : [])
  }
  return latest
}

function buildControlMemberships(data: Uint8Array): Map<string, number[]> {
  const occurrences = new Map<string, number[]>()
  const membershipField = [0x04, 0x0c, 0x00, 0x00]
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 90 >= data.length || data[offset + 4] !== 0x0c) continue
    const length = data[offset + 8]
    if (length < 1 || length > 40) continue
    const nameBytes = data.subarray(offset + 9, offset + 9 + length)
    if (!isPrintable(nameBytes)) continue
    let recordEnd = Math.min(data.length, offset + 400)
    for (const nextRecord of findAll(data, DA_RECORD, offset + 4, recordEnd)) {
      recordEnd = nextRecord
      break
    }
    let mask: number | undefined
    for (const field of findAll(data, membershipField, offset + 40, recordEnd)) {
      const valueOffset = field + membershipField.length
      mask =
        data[valueOffset] |
        (data[valueOffset + 1] << 8) |
        (data[valueOffset + 2] << 16) |
        (data[valueOffset + 3] << 24)
      break
    }
    if (mask !== undefined) {
      const name = ascii(data, offset + 9, length)
      const normalized = mask >>> 0
      const masks = occurrences.get(name) ?? []
      masks.push(normalized)
      occurrences.set(name, masks)
    }
  }
  return occurrences
}

function hostNameBefore(
  data: Uint8Array,
  markerOffset: number,
  socketNames: Set<string>,
): string | undefined {
  const start = Math.max(0, markerOffset - 256)
  const record = lastPatternBefore(data, DA_RECORD, start, markerOffset)
  if (record >= 0) {
    for (let relative = 4; relative < 16; relative += 1) {
      const length = data[record + relative]
      const nameStart = record + relative + 1
      if (length < 4 || length > 40 || nameStart + length > markerOffset) continue
      const bytes = data.subarray(nameStart, nameStart + length)
      if (!isPrintable(bytes)) continue
      const name = ascii(data, nameStart, length)
      if (socketNames.has(name)) return name
    }
  }

  const text = textDecoder.decode(data.subarray(start, markerOffset))
  const found = [...text.matchAll(/[0-9]+:[A-Za-z][A-Za-z0-9 /._-]{1,40}/g)].at(-1)?.[0].trim()
  return found && socketNames.has(found) ? found : undefined
}

function addRoute(routes: Map<string, Set<string>>, source: string, dest: string) {
  const destinations = routes.get(source) ?? new Set<string>()
  destinations.add(dest)
  routes.set(source, destinations)
}

function parseCopyRoutes(data: Uint8Array, sockets: SocketMap): Routes {
  const socketNames = new Set<string>()
  for (const byIndex of sockets.values()) {
    for (const name of byIndex.values()) socketNames.add(name)
  }
  const routes = new Map<string, Set<string>>()
  for (const offset of findAll(data, [0xc8, 0x11, 0x00])) {
    if (offset < 2) continue
    const index = data[offset - 2]
    const type = data[offset - 1]
    if (!isAudioIoType(type)) continue
    const source = hostNameBefore(data, offset, socketNames)
    const dest = resolveSocket(sockets, type, index)
    if (source && dest) addRoute(routes, source, dest)
  }
  for (const offset of findAll(data, [0xff, 0x12, 0x00])) {
    if (offset < 2) continue
    const index = data[offset - 2]
    const type = data[offset - 1]
    if (index === 0xff || !isAudioIoType(type)) continue
    const source = resolveSocket(sockets, type, index)
    const dest = hostNameBefore(data, offset, socketNames)
    if (source && dest) addRoute(routes, source, dest)
  }
  return new Map(
    [...routes].map(([source, destinations]) => [
      source,
      [...destinations].sort(naturalCompare),
    ]),
  )
}

function buildLinkGroups(data: Uint8Array): Map<string, string[]> {
  const latest = new Map<string, string>()
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 100 >= data.length || data[offset + 4] !== 0x12) continue
    const length = data[offset + 8]
    if (length < 2 || length > 40) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!bytes.includes(0x3a) || !isPrintable(bytes)) continue
    const bodyStart = offset + 9 + 32
    const bodyEnd = Math.min(data.length, bodyStart + 120)
    for (const marker of findAll(data, SOCKET_LINK_MARKER, bodyStart, bodyEnd)) {
      if (marker < bodyStart + 2) continue
      const key = `${data[marker - 2]}:${data[marker - 1]}`
      if (key !== '0:0') latest.set(ascii(data, offset + 9, length), key)
      break
    }
  }

  const byKey = new Map<string, string[]>()
  for (const [name, key] of latest) {
    const names = byKey.get(key) ?? []
    names.push(name)
    byKey.set(key, names)
  }
  const result = new Map<string, string[]>()
  for (const names of byKey.values()) {
    names.sort(naturalCompare)
    for (const name of names) result.set(name, names)
  }
  return result
}

function buildFxNames(data: Uint8Array, recordLength: number): Map<number, string> {
  const result = new Map<number, string>()
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 9 >= data.length || data[offset + 5] !== 0x0e) continue
    const index = data[offset + 4]
    const length = data[offset + 8]
    if (length < 2 || length > 40) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (!bytes.includes(0x3a) && isPrintable(bytes) && !result.has(index)) {
      result.set(index, ascii(data, offset + 9, length))
    }
  }
  return result
}

function buildDirectOuts(
  data: Uint8Array,
  sockets: SocketMap,
  recordLength: number,
): Routes {
  const fxNames = buildFxNames(data, recordLength)
  const links = buildLinkGroups(data)
  const latest = new Map<string, string[]>()
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 92 >= data.length || data[offset + 4] !== 0x0e) continue
    const length = data[offset + 8]
    if (length < 1 || length > 40) continue
    const bytes = data.subarray(offset + 9, offset + 9 + length)
    if (bytes.includes(0x3a) || !isPrintable(bytes)) continue
    if (!matches(data, offset + 86, [0x3c, 0x0e, 0x00, 0x00, 0x00, 0x00])) {
      continue
    }
    const name = ascii(data, offset + 9, length)
    const index = data[offset + 84]
    const type = data[offset + 85]
    const destinations: string[] = []
    if (type === 0x0e) {
      const fx = fxNames.get(index)
      if (fx) destinations.push(fx)
      for (const bank of [0x2b, 0x5d]) {
        const socket = resolveSocket(sockets, bank, 16 + index)
        if (socket) {
          destinations.push(socket)
          break
        }
      }
    } else {
      const socket = resolveSocket(sockets, type, index)
      if (socket) destinations.push(socket)
    }
    if (destinations.length) latest.set(name, destinations)
  }

  return new Map(
    [...latest].map(([name, destinations]) => {
      const expanded: string[] = []
      for (const destination of destinations) {
        for (const linked of links.get(destination) ?? [destination]) {
          if (!expanded.includes(linked)) expanded.push(linked)
        }
      }
      return [name, expanded]
    }),
  )
}

function buildSources(
  sockets: SocketMap,
  portNames: Map<number, string>,
  socketToChannel: Map<string, string>,
  copyRoutes: Routes,
  directOuts: Routes,
): SourceRow[] {
  const prefixToPort = new Map<string, string>()
  const plainByType = new Map<number, { portName: string; names: Set<string> }>()
  const nameToPlainType = new Map<string, number>()
  for (const [type, byIndex] of sockets) {
    const portName = portNames.get(type)
    if (!portName || portName === 'Input Channels') continue
    for (const socket of byIndex.values()) {
      if (!socket.includes(':')) {
        const group = plainByType.get(type) ?? { portName, names: new Set<string>() }
        group.names.add(socket)
        plainByType.set(type, group)
        const previous = nameToPlainType.get(socket)
        if (previous === undefined || type < previous) nameToPlainType.set(socket, type)
        continue
      }
      const prefix = socketPrefix(socket)
      if (!prefixToPort.has(prefix)) prefixToPort.set(prefix, portName)
    }
  }

  const allSockets = new Set<string>()
  for (const byIndex of sockets.values()) {
    for (const socket of byIndex.values()) allSockets.add(socket)
  }
  const kindsByPrefix = new Map<string, Set<string>>()
  const plainTypesInUse = new Set<number>()
  for (const source of copyRoutes.keys()) {
    if (!source.includes(':')) {
      const type = nameToPlainType.get(source)
      if (type !== undefined) plainTypesInUse.add(type)
      continue
    }
    const prefix = socketPrefix(source)
    const kinds = kindsByPrefix.get(prefix) ?? new Set<string>()
    kinds.add(socketKind(source))
    kindsByPrefix.set(prefix, kinds)
  }

  const pushSource = (port: string, input: string, index: number, result: SourceRow[]) => {
    const channel = socketToChannel.get(input) ?? ''
    result.push({
      port,
      number: index + 1,
      input,
      channel,
      copyAudio: copyRoutes.get(input) ?? [],
      directOuts: channel ? (directOuts.get(channel) ?? []) : [],
    })
  }

  const prefixes = [...kindsByPrefix.keys()].sort(naturalCompare)
  const result: SourceRow[] = []
  for (const prefix of prefixes) {
    const kinds = kindsByPrefix.get(prefix)!
    const candidates = [...allSockets]
      .filter(
        (socket) =>
          socket.includes(':') &&
          socketPrefix(socket) === prefix &&
          kinds.has(socketKind(socket)),
      )
      .sort(naturalCompare)
    const portName = prefixToPort.get(prefix)
    candidates.forEach((input, index) => {
      pushSource(portName ? `${prefix}:${portName}` : `${prefix}:`, input, index, result)
    })
  }

  for (const type of [...plainTypesInUse].sort((a, b) => a - b)) {
    const group = plainByType.get(type)
    if (!group) continue
    const kinds = new Set<string>()
    for (const source of copyRoutes.keys()) {
      if (nameToPlainType.get(source) === type) kinds.add(socketKind(source))
    }
    const candidates = [...group.names]
      .filter((socket) => kinds.has(socketKind(socket)))
      .sort(naturalCompare)
    candidates.forEach((input, index) => {
      pushSource(`${type - 0x1c}:${group.portName}`, input, index, result)
    })
  }
  return result
}

function emptyRow(id: string, name: string): PatchRow {
  return {
    id,
    name,
    inputs: [],
    mergeInputs: [],
    insertA: [],
    insertB: [],
    outputs: [],
    copyAudio: [],
    members: [],
  }
}

function buildSections(
  catalog: Map<number, CatalogEntry[]>,
  channelToInputs: Map<string, string[]>,
  channelInputOccurrences: Map<string, string[][]>,
  fallbackInputs: Map<string, string[]>,
  copyRoutes: Routes,
  directOuts: Routes,
  inserts: Map<string, { insertA: string[]; insertB: string[] }>,
  mergeInputs: Map<string, string[]>,
  physicalSockets: Set<string>,
  controlMemberships: Map<string, number[]>,
): Record<PatchSectionName, PatchRow[]> {
  const sections: Record<PatchSectionName, PatchRow[]> = {
    'Input Channels': [],
    'Aux Outputs': [],
    'Group Outputs': [],
    'Matrix Inputs': [],
    'Matrix Outputs': [],
    'Control Groups': [],
  }
  const routedCatalogTypes = new Set([0x01, 0x0f])
  const routeCounts = new Map<string, number>()
  for (const [type, entries] of catalog) {
    if (!routedCatalogTypes.has(type)) continue
    for (const entry of entries) {
      routeCounts.set(entry.name, (routeCounts.get(entry.name) ?? 0) + 1)
    }
  }
  const routeIndexes = new Map<string, number>()
  const inputsForEntry = (name: string): string[] => {
    const occurrences = channelInputOccurrences.get(name) ?? []
    const wanted = routeCounts.get(name) ?? 1
    const candidates = occurrences.slice(-wanted)
    const index = routeIndexes.get(name) ?? 0
    routeIndexes.set(name, index + 1)
    return candidates[index] ?? channelToInputs.get(name) ?? []
  }

  for (const [type, section, prefix] of sectionTypes) {
    for (const entry of catalog.get(type) ?? []) {
      const numericPosition = entry.position || entry.index + 1
      const suffix = entry.stereo && ['Input Channels', 'Aux Outputs', 'Group Outputs'].includes(section)
        ? 's'
        : ''
      const id = `${prefix}${numericPosition}${suffix}`
      const row = emptyRow(id, entry.name)
      row.outputs = directOuts.get(entry.name) ?? []
      row.insertA = inserts.get(entry.name)?.insertA ?? []
      row.insertB = inserts.get(entry.name)?.insertB ?? []
      if (section === 'Input Channels') {
        const current = inputsForEntry(entry.name)
        const fallbacks = fallbackInputs.get(entry.name) ?? []
        const source = current[0]
        if (source?.toLowerCase() === entry.name.toLowerCase()) {
          if (source !== entry.name) {
            row.inputs = [entry.name]
          } else {
            const alternateCase = fallbacks.find(
              (value) =>
                value !== source &&
                value.toLowerCase() === source.toLowerCase(),
            )
            if (alternateCase) {
              row.inputs = [alternateCase]
            } else {
              const physical = fallbacks.filter((value) =>
                physicalSockets.has(value),
              )
              row.inputs = physical.length ? physical : current
            }
          }
        } else {
          row.inputs = current
        }
      } else if (section === 'Matrix Inputs') {
        if (/^mtx in \d+$/i.test(entry.name)) {
          row.inputs = []
          sections[section].push(row)
          continue
        }
        const resolvedInputs = inputsForEntry(entry.name)
        const candidates = resolvedInputs.length || channelInputOccurrences.has(entry.name)
          ? resolvedInputs
          : (fallbackInputs.get(entry.name) ?? [])
        const input =
          candidates.find((candidate) => !physicalSockets.has(candidate)) ??
          candidates[0]
        if (input && !physicalSockets.has(input)) {
          const leg = entry.name.match(/([LR])$/)?.[1]
          row.inputs =
            leg && !input.endsWith(` ${leg}`) ? [`${input} ${leg}`] : [input]
        } else {
          row.inputs = input ? [input] : []
        }
      } else if (
        section === 'Aux Outputs' ||
        section === 'Group Outputs' ||
        section === 'Matrix Outputs'
      ) {
        row.mergeInputs = mergeInputs.get(entry.name) ?? []
      }
      if (section === 'Input Channels') {
        row.copyAudio = row.inputs.flatMap((input) => copyRoutes.get(input) ?? [])
      }
      sections[section].push(row)
    }
  }

  const memberRows = [
    ...sections['Input Channels'],
    ...sections['Aux Outputs'],
    ...sections['Group Outputs'],
    ...sections['Matrix Outputs'],
  ]
  const membershipCounts = new Map<string, number>()
  for (const row of memberRows) {
    membershipCounts.set(row.name, (membershipCounts.get(row.name) ?? 0) + 1)
  }
  const membershipIndexes = new Map<string, number>()
  const membershipByRow = new Map<PatchRow, number>()
  for (const row of memberRows) {
    const masks = controlMemberships.get(row.name) ?? []
    const wanted = membershipCounts.get(row.name) ?? 1
    const candidates = masks.slice(-wanted)
    const index = membershipIndexes.get(row.name) ?? 0
    membershipIndexes.set(row.name, index + 1)
    membershipByRow.set(row, candidates[index] ?? masks.at(-1) ?? 0)
  }
  sections['Control Groups'].forEach((controlGroup, index) => {
    const bit = 1 << index
    controlGroup.members = [...new Set(memberRows
      .filter((row) => ((membershipByRow.get(row) ?? 0) & bit) !== 0)
      .map((row) => row.name))]
  })
  return sections
}

export function parseSession(buffer: ArrayBuffer, fileName: string): SessionModel {
  const data = new Uint8Array(buffer)
  if (data.length < 100) {
    throw new Error('This file does not look like a supported DiGiCo session.')
  }

  const profile = parseProfile(data)
  const sockets = buildSocketMap(data, profile.catalogRecordLength)
  if (!sockets.size) {
    throw new Error('No DiGiCo Audio I/O records were found in this session.')
  }
  const portNames = buildPortNames(data)
  const physicalSockets = new Set(
    [...sockets.values()].flatMap((byIndex) => [...byIndex.values()]),
  )
  const catalog = buildCatalog(data, profile.catalogRecordLength)
  const references = buildReferenceNames(data, profile.catalogRecordLength)
  const stereoNames = buildStereoNames(data, profile.catalogRecordLength)
  const {
    socketToChannel,
    channelToInputs,
    channelInputOccurrences,
    fallbackInputs,
  } = buildInputAssignments(
    data,
    sockets,
    references,
    stereoNames,
  )
  const copyRoutes = parseCopyRoutes(data, sockets)
  const directOuts = buildDirectOuts(data, sockets, profile.catalogRecordLength)
  const inserts = buildInserts(data, sockets, references)
  const mergeInputs = buildMergeInputs(
    data,
    sockets,
    references,
    stereoNames,
  )
  const controlMemberships = buildControlMemberships(data)
  const sources = buildSources(
    sockets,
    portNames,
    socketToChannel,
    copyRoutes,
    directOuts,
  )
  const sections = buildSections(
    catalog,
    channelToInputs,
    channelInputOccurrences,
    fallbackInputs,
    copyRoutes,
    directOuts,
    inserts,
    mergeInputs,
    physicalSockets,
    controlMemberships,
  )
  const ioPorts = [...portNames]
    .filter(([type]) => type >= 0x1c && type <= 0x2b)
    .map(([type, name]) => ({ number: type - 0x1c, name }))
    .sort((a, b) => a.number - b.number)

  const copyCount = [...copyRoutes.values()].reduce(
    (sum, destinations) => sum + destinations.length,
    0,
  )
  const directCount = sources.reduce(
    (sum, source) => sum + source.directOuts.length,
    0,
  )
  const stem = fileName.replace(/\.ses$/i, '')
  const warnings: string[] = []
  if (!sections['Input Channels'].length) {
    warnings.push('No input-channel catalog was found.')
  } else if (
    copyCount === 0 &&
    directCount === 0 &&
    !sections['Input Channels'].some((row) => row.inputs.length > 0)
  ) {
    warnings.push(
      'These channels are unassigned. This session has no stored input routes, copy audio, or direct outputs, so the patch columns are blank.',
    )
  }

  return {
    fileName,
    sessionName: stem,
    profile,
    ioPorts,
    sources,
    sections,
    stats: {
      bytes: data.length,
      sockets: new Set(
        [...sockets.values()].flatMap((byIndex) => [...byIndex.values()]),
      ).size,
      namedSockets: socketToChannel.size,
      copyRoutes: copyCount,
      directOutputs: directCount,
    },
    warnings,
  }
}
