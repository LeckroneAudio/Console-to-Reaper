import { readFileSync } from "node:fs";
import { basename } from "node:path";
const textDecoder = new TextDecoder("windows-1252");
const DA_RECORD = [218, 0, 58, 0];
const CB_RECORD_PREFIX = [203, 0];
const CA_RECORD = [202, 0, 108];
const INPUT_ASSIGN_SUFFIX = [0, 1, 1, 0, 0, 0];
const SOCKET_LINK_MARKER = [226, 18, 0, 0, 0, 0];
const SOCKET_NAME_FIELD = 32;
const SOCKET_TRAILER = [1, 255, 255, 255, 255, 64, 14];
const consoleModels = {
  Q1: "Q12",
  Q2: "Q225",
  Q3: "Q338",
  Q5: "Q5",
  SDQ: "Q7",
  Q8: "Q852",
  SD5: "SD5",
  SD7: "SD7",
  SD8: "SD10",
  SD9: "SD11",
  SDC: "SD12"
};
const catalogRecordLengths = [80, 88, 120, 121];
const sectionTypes = [
  [1, "Input Channels", ""],
  [5, "Aux Outputs", "A"],
  [6, "Group Outputs", "G"],
  [15, "Matrix Inputs", "Mi"],
  [16, "Matrix Outputs", "M"],
  [10, "Control Groups", "CG"]
];
function matches(data, offset, pattern) {
  if (offset < 0 || offset + pattern.length > data.length) return false;
  for (let i = 0; i < pattern.length; i += 1) {
    if (data[offset + i] !== pattern[i]) return false;
  }
  return true;
}
function* findAll(data, pattern, start = 0, end = data.length) {
  let offset = start;
  while (offset < end) {
    const found = data.indexOf(pattern[0], offset);
    if (found < 0 || found + pattern.length > end) return;
    if (matches(data, found, pattern)) yield found;
    offset = found + 1;
  }
}
function* findCatalogRecords(data, recordLength) {
  for (const offset of findAll(data, CB_RECORD_PREFIX)) {
    if (data[offset + 2] === recordLength) yield offset;
  }
}
function parseProfile(data) {
  if (ascii(data, 0, 11) !== "DiGiCo") {
    throw new Error("This file does not look like a DiGiCo session.");
  }
  const modelCode = ascii(data, 11, 4);
  const formatRevision = ascii(data, 20, 5);
  const candidates = catalogRecordLengths.map((recordLength) => {
    let channels = 0;
    let sockets = 0;
    for (const offset of findCatalogRecords(data, recordLength)) {
      if (offset + 9 >= data.length) continue;
      const type = data[offset + 5];
      const nameLength = data[offset + 8];
      if (nameLength > 40 || offset + 9 + nameLength > data.length) continue;
      const name = data.subarray(offset + 9, offset + 9 + nameLength);
      if (!isPrintable(name)) continue;
      if (type === 1) channels += 1;
      if (isAudioSocketRecord(data, offset)) sockets += 1;
    }
    return { recordLength, channels, sockets };
  });
  const layout = candidates.filter(({ channels, sockets }) => channels > 0 && sockets > 0).sort((a, b) => b.channels + b.sockets - (a.channels + a.sockets))[0];
  if (!layout) {
    throw new Error(
      `The DiGiCo session header was recognized (${modelCode || "unknown"}/${formatRevision || "unknown"}), but its catalog record layout is not supported.`
    );
  }
  return {
    console: consoleModels[modelCode] || modelCode || "Unknown DiGiCo",
    modelCode,
    formatRevision,
    catalogRecordLength: layout.recordLength
  };
}
function lastPatternBefore(data, pattern, start, end) {
  let last = -1;
  for (const offset of findAll(data, pattern, start, end)) last = offset;
  return last;
}
function isPrintable(bytes) {
  return bytes.every((value) => value >= 32 && value !== 127);
}
function ascii(data, start, length) {
  return textDecoder.decode(data.subarray(start, start + length)).trim();
}
function naturalCompare(a, b) {
  return a.localeCompare(b, void 0, { numeric: true, sensitivity: "base" });
}
function socketPrefix(socket) {
  return socket.includes(":") ? socket.split(":", 1)[0] : socket;
}
function socketKind(socket) {
  const suffix = socket.includes(":") ? socket.slice(socket.indexOf(":") + 1) : socket;
  return suffix.replace(/\s*\d+$/, "").trim();
}
function isAudioIoType(type) {
  return type >= 28 && type <= 93;
}
function isAudioSocketRecord(data, offset) {
  return isAudioIoType(data[offset + 5]) && matches(data, offset + 9 + SOCKET_NAME_FIELD, SOCKET_TRAILER);
}
function resolveSocket(sockets, type, index) {
  return sockets.get(type)?.get(index);
}
function buildSocketMap(data, recordLength) {
  const result = /* @__PURE__ */ new Map();
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 49 >= data.length) continue;
    const index = data[offset + 4];
    const type = data[offset + 5];
    const length = data[offset + 8];
    if (length < 2 || length > 40 || offset + 9 + length > data.length) continue;
    if (!isAudioSocketRecord(data, offset)) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(bytes)) continue;
    const byIndex = result.get(type) ?? /* @__PURE__ */ new Map();
    if (!byIndex.has(index)) byIndex.set(index, ascii(data, offset + 9, length));
    result.set(type, byIndex);
  }
  return result;
}
function buildPortNames(data) {
  const result = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, CA_RECORD)) {
    if (offset + 6 >= data.length) continue;
    const type = data[offset + 4];
    const length = data[offset + 5];
    if (length < 1 || length > 40 || offset + 6 + length > data.length) continue;
    const bytes = data.subarray(offset + 6, offset + 6 + length);
    if (!isPrintable(bytes) || result.has(type)) continue;
    result.set(type, ascii(data, offset + 6, length));
  }
  return result;
}
function buildCatalog(data, recordLength) {
  const wantedTypes = new Set(sectionTypes.map(([type]) => type));
  const byType = /* @__PURE__ */ new Map();
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue;
    const index = data[offset + 4];
    const type = data[offset + 5];
    const position = data[offset + 6];
    const length = data[offset + 8];
    if (!wantedTypes.has(type) || length > 32) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(bytes)) continue;
    const entries = byType.get(type) ?? /* @__PURE__ */ new Map();
    entries.set(position, {
      index,
      position,
      name: ascii(data, offset + 9, length),
      stereo: data[offset + 41] === 2
    });
    byType.set(type, entries);
  }
  const result = /* @__PURE__ */ new Map();
  for (const [type, entries] of byType) {
    result.set(type, [...entries.values()].sort((a, b) => a.position - b.position));
  }
  return result;
}
function buildReferenceNames(data, recordLength) {
  const result = /* @__PURE__ */ new Map();
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue;
    const index = data[offset + 4];
    const type = data[offset + 5];
    const length = data[offset + 8];
    if (length < 1 || length > 32) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(bytes)) continue;
    const byIndex = result.get(type) ?? /* @__PURE__ */ new Map();
    if (!byIndex.has(index)) byIndex.set(index, ascii(data, offset + 9, length));
    result.set(type, byIndex);
  }
  return result;
}
function buildStereoNames(data, recordLength) {
  const result = /* @__PURE__ */ new Set();
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 42 >= data.length) continue;
    const length = data[offset + 8];
    if (length < 1 || length > 32 || data[offset + 41] !== 2) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (isPrintable(bytes) && !bytes.includes(58)) {
      result.add(ascii(data, offset + 9, length));
    }
  }
  return result;
}
function nextSocket(sockets, socket) {
  for (const byIndex of sockets.values()) {
    for (const [index, name] of byIndex) {
      if (name === socket) return byIndex.get(index + 1);
    }
  }
  return void 0;
}
function buildInputAssignments(data, sockets, references, stereoNames) {
  const socketNames = new Set(
    [...sockets.values()].flatMap((byIndex) => [...byIndex.values()])
  );
  const votes = /* @__PURE__ */ new Map();
  const physicalVotes = /* @__PURE__ */ new Map();
  const latestInputs = /* @__PURE__ */ new Map();
  const inputOccurrences = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 300 >= data.length || data[offset + 4] !== 1) continue;
    const length = data[offset + 8];
    if (length > 40) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(bytes)) continue;
    const name = ascii(data, offset + 9, length);
    const regionEnd = Math.min(data.length, offset + 300);
    let recordInputs = [];
    for (const marker of findAll(data, INPUT_ASSIGN_SUFFIX, offset, regionEnd)) {
      if (marker < offset + 2) continue;
      const index = data[marker - 2];
      const type = data[marker - 1];
      let source = resolveSocket(sockets, type, index) ?? references.get(type)?.get(index);
      if (!source) continue;
      const leg = name.match(/ ([LR])$/)?.[1];
      if (leg && !socketNames.has(source) && !source.endsWith(` ${leg}`)) {
        source = `${source} ${leg}`;
      } else if (stereoNames.has(name) && (stereoNames.has(source) || type === 14) && !source.endsWith(" L")) {
        source = `${source} L`;
      }
      const bySocket = votes.get(name) ?? /* @__PURE__ */ new Map();
      bySocket.set(source, (bySocket.get(source) ?? 0) + 1);
      const secondaryIndex = data[marker + INPUT_ASSIGN_SUFFIX.length];
      const secondaryType = data[marker + INPUT_ASSIGN_SUFFIX.length + 1];
      const secondary = resolveSocket(sockets, secondaryType, secondaryIndex) ?? references.get(secondaryType)?.get(secondaryIndex);
      if (secondary && secondary !== source) {
        bySocket.set(secondary, (bySocket.get(secondary) ?? 0) + 1);
      }
      votes.set(name, bySocket);
      recordInputs = secondary && secondary !== source ? [source, secondary] : [source];
      latestInputs.set(name, recordInputs);
      if (socketNames.has(source)) {
        const physical = physicalVotes.get(name) ?? /* @__PURE__ */ new Map();
        physical.set(source, (physical.get(source) ?? 0) + 1);
        if (secondary && socketNames.has(secondary)) {
          physical.set(secondary, (physical.get(secondary) ?? 0) + 1);
        }
        physicalVotes.set(name, physical);
      }
      break;
    }
    if (!recordInputs.length) latestInputs.set(name, []);
    const occurrences = inputOccurrences.get(name) ?? [];
    occurrences.push(recordInputs);
    inputOccurrences.set(name, occurrences);
  }
  const socketClaims = /* @__PURE__ */ new Map();
  const channelToInputs = latestInputs;
  const fallbackInputs = new Map(
    [...votes].map(([name, bySocket]) => [
      name,
      [...bySocket].sort((a, b) => b[1] - a[1]).map(([source]) => source)
    ])
  );
  for (const [name, bySocket] of physicalVotes) {
    const ranked = [...bySocket].sort((a, b) => b[1] - a[1]);
    const [source, count] = ranked[0];
    const claim = (candidate) => {
      const previous = socketClaims.get(candidate);
      if (!previous || count > previous.votes) {
        socketClaims.set(candidate, { name, votes: count });
      }
    };
    claim(source);
    if (stereoNames.has(name)) {
      const right = nextSocket(sockets, source);
      if (right) {
        claim(right);
      }
    }
  }
  return {
    socketToChannel: new Map(
      [...socketClaims].map(([socket, claim]) => [socket, claim.name])
    ),
    channelToInputs,
    channelInputOccurrences: inputOccurrences,
    fallbackInputs
  };
}
function buildInserts(data, sockets, references) {
  const latest = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 206 >= data.length || data[offset + 4] !== 8) continue;
    const length = data[offset + 8];
    if (length < 1 || length > 40) continue;
    const nameBytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(nameBytes)) continue;
    const readValues = (pairOffsets) => {
      const values = [];
      for (const pairOffset of pairOffsets) {
        const index = data[pairOffset];
        const type = data[pairOffset + 1];
        const value = resolveSocket(sockets, type, index) ?? references.get(type)?.get(index);
        if (value && !values.includes(value)) values.push(value);
      }
      return values;
    };
    latest.set(ascii(data, offset + 9, length), {
      insertA: readValues([offset + 124, offset + 132]),
      insertB: readValues([offset + 196, offset + 204])
    });
  }
  return latest;
}
function buildMergeInputs(data, sockets, references, stereoNames) {
  const latest = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 102 >= data.length || data[offset + 4] !== 11) continue;
    const length = data[offset + 8];
    if (length < 1 || length > 40) continue;
    const nameBytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(nameBytes)) continue;
    const name = ascii(data, offset + 9, length);
    const index = data[offset + 100];
    const type = data[offset + 101];
    let source = resolveSocket(sockets, type, index) ?? references.get(type)?.get(index);
    if (source && stereoNames.has(name) && stereoNames.has(source) && !source.endsWith(" L")) {
      source = `${source} L`;
    }
    latest.set(name, source ? [source] : []);
  }
  return latest;
}
function buildControlMemberships(data) {
  const occurrences = /* @__PURE__ */ new Map();
  const membershipField = [4, 12, 0, 0];
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 90 >= data.length || data[offset + 4] !== 12) continue;
    const length = data[offset + 8];
    if (length < 1 || length > 40) continue;
    const nameBytes = data.subarray(offset + 9, offset + 9 + length);
    if (!isPrintable(nameBytes)) continue;
    let recordEnd = Math.min(data.length, offset + 400);
    for (const nextRecord of findAll(data, DA_RECORD, offset + 4, recordEnd)) {
      recordEnd = nextRecord;
      break;
    }
    let mask;
    for (const field of findAll(data, membershipField, offset + 40, recordEnd)) {
      const valueOffset = field + membershipField.length;
      mask = data[valueOffset] | data[valueOffset + 1] << 8 | data[valueOffset + 2] << 16 | data[valueOffset + 3] << 24;
      break;
    }
    if (mask !== void 0) {
      const name = ascii(data, offset + 9, length);
      const normalized = mask >>> 0;
      const masks = occurrences.get(name) ?? [];
      masks.push(normalized);
      occurrences.set(name, masks);
    }
  }
  return occurrences;
}
function hostNameBefore(data, markerOffset, socketNames) {
  const start = Math.max(0, markerOffset - 256);
  const record = lastPatternBefore(data, DA_RECORD, start, markerOffset);
  if (record >= 0) {
    for (let relative = 4; relative < 16; relative += 1) {
      const length = data[record + relative];
      const nameStart = record + relative + 1;
      if (length < 4 || length > 40 || nameStart + length > markerOffset) continue;
      const bytes = data.subarray(nameStart, nameStart + length);
      if (!isPrintable(bytes)) continue;
      const name = ascii(data, nameStart, length);
      if (socketNames.has(name)) return name;
    }
  }
  const text = textDecoder.decode(data.subarray(start, markerOffset));
  const found = [...text.matchAll(/[0-9]+:[A-Za-z][A-Za-z0-9 /._-]{1,40}/g)].at(-1)?.[0].trim();
  return found && socketNames.has(found) ? found : void 0;
}
function addRoute(routes, source, dest) {
  const destinations = routes.get(source) ?? /* @__PURE__ */ new Set();
  destinations.add(dest);
  routes.set(source, destinations);
}
function parseCopyRoutes(data, sockets) {
  const socketNames = /* @__PURE__ */ new Set();
  for (const byIndex of sockets.values()) {
    for (const name of byIndex.values()) socketNames.add(name);
  }
  const routes = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, [200, 17, 0])) {
    if (offset < 2) continue;
    const index = data[offset - 2];
    const type = data[offset - 1];
    if (!isAudioIoType(type)) continue;
    const source = hostNameBefore(data, offset, socketNames);
    const dest = resolveSocket(sockets, type, index);
    if (source && dest) addRoute(routes, source, dest);
  }
  for (const offset of findAll(data, [255, 18, 0])) {
    if (offset < 2) continue;
    const index = data[offset - 2];
    const type = data[offset - 1];
    if (index === 255 || !isAudioIoType(type)) continue;
    const source = resolveSocket(sockets, type, index);
    const dest = hostNameBefore(data, offset, socketNames);
    if (source && dest) addRoute(routes, source, dest);
  }
  return new Map(
    [...routes].map(([source, destinations]) => [
      source,
      [...destinations].sort(naturalCompare)
    ])
  );
}
function buildLinkGroups(data) {
  const latest = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 100 >= data.length || data[offset + 4] !== 18) continue;
    const length = data[offset + 8];
    if (length < 2 || length > 40) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!bytes.includes(58) || !isPrintable(bytes)) continue;
    const bodyStart = offset + 9 + 32;
    const bodyEnd = Math.min(data.length, bodyStart + 120);
    for (const marker of findAll(data, SOCKET_LINK_MARKER, bodyStart, bodyEnd)) {
      if (marker < bodyStart + 2) continue;
      const key = `${data[marker - 2]}:${data[marker - 1]}`;
      if (key !== "0:0") latest.set(ascii(data, offset + 9, length), key);
      break;
    }
  }
  const byKey = /* @__PURE__ */ new Map();
  for (const [name, key] of latest) {
    const names = byKey.get(key) ?? [];
    names.push(name);
    byKey.set(key, names);
  }
  const result = /* @__PURE__ */ new Map();
  for (const names of byKey.values()) {
    names.sort(naturalCompare);
    for (const name of names) result.set(name, names);
  }
  return result;
}
function buildFxNames(data, recordLength) {
  const result = /* @__PURE__ */ new Map();
  for (const offset of findCatalogRecords(data, recordLength)) {
    if (offset + 9 >= data.length || data[offset + 5] !== 14) continue;
    const index = data[offset + 4];
    const length = data[offset + 8];
    if (length < 2 || length > 40) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (!bytes.includes(58) && isPrintable(bytes) && !result.has(index)) {
      result.set(index, ascii(data, offset + 9, length));
    }
  }
  return result;
}
function buildDirectOuts(data, sockets, recordLength) {
  const fxNames = buildFxNames(data, recordLength);
  const links = buildLinkGroups(data);
  const latest = /* @__PURE__ */ new Map();
  for (const offset of findAll(data, DA_RECORD)) {
    if (offset + 92 >= data.length || data[offset + 4] !== 14) continue;
    const length = data[offset + 8];
    if (length < 1 || length > 40) continue;
    const bytes = data.subarray(offset + 9, offset + 9 + length);
    if (bytes.includes(58) || !isPrintable(bytes)) continue;
    if (!matches(data, offset + 86, [60, 14, 0, 0, 0, 0])) {
      continue;
    }
    const name = ascii(data, offset + 9, length);
    const index = data[offset + 84];
    const type = data[offset + 85];
    const destinations = [];
    if (type === 14) {
      const fx = fxNames.get(index);
      if (fx) destinations.push(fx);
      for (const bank of [43, 93]) {
        const socket = resolveSocket(sockets, bank, 16 + index);
        if (socket) {
          destinations.push(socket);
          break;
        }
      }
    } else {
      const socket = resolveSocket(sockets, type, index);
      if (socket) destinations.push(socket);
    }
    if (destinations.length) latest.set(name, destinations);
  }
  return new Map(
    [...latest].map(([name, destinations]) => {
      const expanded = [];
      for (const destination of destinations) {
        for (const linked of links.get(destination) ?? [destination]) {
          if (!expanded.includes(linked)) expanded.push(linked);
        }
      }
      return [name, expanded];
    })
  );
}
function buildSources(sockets, portNames, socketToChannel, copyRoutes, directOuts) {
  const prefixToPort = /* @__PURE__ */ new Map();
  const plainByType = /* @__PURE__ */ new Map();
  const nameToPlainType = /* @__PURE__ */ new Map();
  for (const [type, byIndex] of sockets) {
    const portName = portNames.get(type);
    if (!portName || portName === "Input Channels") continue;
    for (const socket of byIndex.values()) {
      if (!socket.includes(":")) {
        const group = plainByType.get(type) ?? { portName, names: /* @__PURE__ */ new Set() };
        group.names.add(socket);
        plainByType.set(type, group);
        const previous = nameToPlainType.get(socket);
        if (previous === void 0 || type < previous) nameToPlainType.set(socket, type);
        continue;
      }
      const prefix = socketPrefix(socket);
      if (!prefixToPort.has(prefix)) prefixToPort.set(prefix, portName);
    }
  }
  const allSockets = /* @__PURE__ */ new Set();
  for (const byIndex of sockets.values()) {
    for (const socket of byIndex.values()) allSockets.add(socket);
  }
  const kindsByPrefix = /* @__PURE__ */ new Map();
  const plainTypesInUse = /* @__PURE__ */ new Set();
  for (const source of copyRoutes.keys()) {
    if (!source.includes(":")) {
      const type = nameToPlainType.get(source);
      if (type !== void 0) plainTypesInUse.add(type);
      continue;
    }
    const prefix = socketPrefix(source);
    const kinds = kindsByPrefix.get(prefix) ?? /* @__PURE__ */ new Set();
    kinds.add(socketKind(source));
    kindsByPrefix.set(prefix, kinds);
  }
  const pushSource = (port, input, index, result2) => {
    const channel = socketToChannel.get(input) ?? "";
    result2.push({
      port,
      number: index + 1,
      input,
      channel,
      copyAudio: copyRoutes.get(input) ?? [],
      directOuts: channel ? directOuts.get(channel) ?? [] : []
    });
  };
  const prefixes = [...kindsByPrefix.keys()].sort(naturalCompare);
  const result = [];
  for (const prefix of prefixes) {
    const kinds = kindsByPrefix.get(prefix);
    const candidates = [...allSockets].filter(
      (socket) => socket.includes(":") && socketPrefix(socket) === prefix && kinds.has(socketKind(socket))
    ).sort(naturalCompare);
    const portName = prefixToPort.get(prefix);
    candidates.forEach((input, index) => {
      pushSource(portName ? `${prefix}:${portName}` : `${prefix}:`, input, index, result);
    });
  }
  for (const type of [...plainTypesInUse].sort((a, b) => a - b)) {
    const group = plainByType.get(type);
    if (!group) continue;
    const kinds = /* @__PURE__ */ new Set();
    for (const source of copyRoutes.keys()) {
      if (nameToPlainType.get(source) === type) kinds.add(socketKind(source));
    }
    const candidates = [...group.names].filter((socket) => kinds.has(socketKind(socket))).sort(naturalCompare);
    candidates.forEach((input, index) => {
      pushSource(`${type - 28}:${group.portName}`, input, index, result);
    });
  }
  return result;
}
function emptyRow(id, name) {
  return {
    id,
    name,
    inputs: [],
    mergeInputs: [],
    insertA: [],
    insertB: [],
    outputs: [],
    copyAudio: [],
    members: []
  };
}
function buildSections(catalog, channelToInputs, channelInputOccurrences, fallbackInputs, copyRoutes, directOuts, inserts, mergeInputs, physicalSockets, controlMemberships) {
  const sections = {
    "Input Channels": [],
    "Aux Outputs": [],
    "Group Outputs": [],
    "Matrix Inputs": [],
    "Matrix Outputs": [],
    "Control Groups": []
  };
  const routedCatalogTypes = /* @__PURE__ */ new Set([1, 15]);
  const routeCounts = /* @__PURE__ */ new Map();
  for (const [type, entries] of catalog) {
    if (!routedCatalogTypes.has(type)) continue;
    for (const entry of entries) {
      routeCounts.set(entry.name, (routeCounts.get(entry.name) ?? 0) + 1);
    }
  }
  const routeIndexes = /* @__PURE__ */ new Map();
  const inputsForEntry = (name) => {
    const occurrences = channelInputOccurrences.get(name) ?? [];
    const wanted = routeCounts.get(name) ?? 1;
    const candidates = occurrences.slice(-wanted);
    const index = routeIndexes.get(name) ?? 0;
    routeIndexes.set(name, index + 1);
    return candidates[index] ?? channelToInputs.get(name) ?? [];
  };
  for (const [type, section, prefix] of sectionTypes) {
    for (const entry of catalog.get(type) ?? []) {
      const numericPosition = entry.position || entry.index + 1;
      const suffix = entry.stereo && ["Input Channels", "Aux Outputs", "Group Outputs"].includes(section) ? "s" : "";
      const id = `${prefix}${numericPosition}${suffix}`;
      const row = emptyRow(id, entry.name);
      row.outputs = directOuts.get(entry.name) ?? [];
      row.insertA = inserts.get(entry.name)?.insertA ?? [];
      row.insertB = inserts.get(entry.name)?.insertB ?? [];
      if (section === "Input Channels") {
        const current = inputsForEntry(entry.name);
        const fallbacks = fallbackInputs.get(entry.name) ?? [];
        const source = current[0];
        if (source?.toLowerCase() === entry.name.toLowerCase()) {
          if (source !== entry.name) {
            row.inputs = [entry.name];
          } else {
            const alternateCase = fallbacks.find(
              (value) => value !== source && value.toLowerCase() === source.toLowerCase()
            );
            if (alternateCase) {
              row.inputs = [alternateCase];
            } else {
              const physical = fallbacks.filter(
                (value) => physicalSockets.has(value)
              );
              row.inputs = physical.length ? physical : current;
            }
          }
        } else {
          row.inputs = current;
        }
      } else if (section === "Matrix Inputs") {
        if (/^mtx in \d+$/i.test(entry.name)) {
          row.inputs = [];
          sections[section].push(row);
          continue;
        }
        const resolvedInputs = inputsForEntry(entry.name);
        const candidates = resolvedInputs.length || channelInputOccurrences.has(entry.name) ? resolvedInputs : fallbackInputs.get(entry.name) ?? [];
        const input = candidates.find((candidate) => !physicalSockets.has(candidate)) ?? candidates[0];
        if (input && !physicalSockets.has(input)) {
          const leg = entry.name.match(/([LR])$/)?.[1];
          row.inputs = leg && !input.endsWith(` ${leg}`) ? [`${input} ${leg}`] : [input];
        } else {
          row.inputs = input ? [input] : [];
        }
      } else if (section === "Aux Outputs" || section === "Group Outputs" || section === "Matrix Outputs") {
        row.mergeInputs = mergeInputs.get(entry.name) ?? [];
      }
      if (section === "Input Channels") {
        row.copyAudio = row.inputs.flatMap((input) => copyRoutes.get(input) ?? []);
      }
      sections[section].push(row);
    }
  }
  const memberRows = [
    ...sections["Input Channels"],
    ...sections["Aux Outputs"],
    ...sections["Group Outputs"],
    ...sections["Matrix Outputs"]
  ];
  const membershipCounts = /* @__PURE__ */ new Map();
  for (const row of memberRows) {
    membershipCounts.set(row.name, (membershipCounts.get(row.name) ?? 0) + 1);
  }
  const membershipIndexes = /* @__PURE__ */ new Map();
  const membershipByRow = /* @__PURE__ */ new Map();
  for (const row of memberRows) {
    const masks = controlMemberships.get(row.name) ?? [];
    const wanted = membershipCounts.get(row.name) ?? 1;
    const candidates = masks.slice(-wanted);
    const index = membershipIndexes.get(row.name) ?? 0;
    membershipIndexes.set(row.name, index + 1);
    membershipByRow.set(row, candidates[index] ?? masks.at(-1) ?? 0);
  }
  sections["Control Groups"].forEach((controlGroup, index) => {
    const bit = 1 << index;
    controlGroup.members = [...new Set(memberRows.filter((row) => ((membershipByRow.get(row) ?? 0) & bit) !== 0).map((row) => row.name))];
  });
  return sections;
}
function parseSession(buffer, fileName) {
  const data = new Uint8Array(buffer);
  if (data.length < 100) {
    throw new Error("This file does not look like a supported DiGiCo session.");
  }
  const profile = parseProfile(data);
  const sockets = buildSocketMap(data, profile.catalogRecordLength);
  if (!sockets.size) {
    throw new Error("No DiGiCo Audio I/O records were found in this session.");
  }
  const portNames = buildPortNames(data);
  const physicalSockets = new Set(
    [...sockets.values()].flatMap((byIndex) => [...byIndex.values()])
  );
  const catalog = buildCatalog(data, profile.catalogRecordLength);
  const references = buildReferenceNames(data, profile.catalogRecordLength);
  const stereoNames = buildStereoNames(data, profile.catalogRecordLength);
  const {
    socketToChannel,
    channelToInputs,
    channelInputOccurrences,
    fallbackInputs
  } = buildInputAssignments(
    data,
    sockets,
    references,
    stereoNames
  );
  const copyRoutes = parseCopyRoutes(data, sockets);
  const directOuts = buildDirectOuts(data, sockets, profile.catalogRecordLength);
  const inserts = buildInserts(data, sockets, references);
  const mergeInputs = buildMergeInputs(
    data,
    sockets,
    references,
    stereoNames
  );
  const controlMemberships = buildControlMemberships(data);
  const sources = buildSources(
    sockets,
    portNames,
    socketToChannel,
    copyRoutes,
    directOuts
  );
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
    controlMemberships
  );
  const ioPorts = [...portNames].filter(([type]) => type >= 28 && type <= 43).map(([type, name]) => ({ number: type - 28, name })).sort((a, b) => a.number - b.number);
  const copyCount = [...copyRoutes.values()].reduce(
    (sum, destinations) => sum + destinations.length,
    0
  );
  const directCount = sources.reduce(
    (sum, source) => sum + source.directOuts.length,
    0
  );
  const stem = fileName.replace(/\.ses$/i, "");
  const warnings = [];
  if (!sections["Input Channels"].length) {
    warnings.push("No input-channel catalog was found.");
  } else if (copyCount === 0 && directCount === 0 && !sections["Input Channels"].some((row) => row.inputs.length > 0)) {
    warnings.push(
      "These channels are unassigned. This session has no stored input routes, copy audio, or direct outputs, so the patch columns are blank."
    );
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
        [...sockets.values()].flatMap((byIndex) => [...byIndex.values()])
      ).size,
      namedSockets: socketToChannel.size,
      copyRoutes: copyCount,
      directOutputs: directCount
    },
    warnings
  };
}
const filePath = process.argv[2];
if (!filePath) {
  console.error("Usage: node digipanion-parse.mjs <session.ses>");
  process.exit(1);
}
try {
  const file = readFileSync(filePath);
  const buffer = file.buffer.slice(
    file.byteOffset,
    file.byteOffset + file.byteLength
  );
  const session = parseSession(buffer, basename(filePath));
  process.stdout.write(`${JSON.stringify(session, null, 2)}
`);
} catch (error) {
  console.error(error instanceof Error ? error.message : "The session could not be parsed.");
  process.exit(1);
}
