import { createLibp2p } from 'libp2p'
import { tcp } from '@libp2p/tcp'
import { noise } from '@libp2p/noise'
import { yamux } from '@libp2p/yamux'
import { identify } from '@libp2p/identify'
import { autoNATv2 } from '@libp2p/autonat-v2'
import { multiaddr } from '@multiformats/multiaddr'
import { peerIdFromString } from '@libp2p/peer-id'
import * as fs from 'node:fs'
import * as path from 'node:path'
import parseArgs from 'minimist'

// OpenTelemetry imports
import { NodeSDK } from '@opentelemetry/sdk-node'
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http'
import { Resource } from '@opentelemetry/resources'
import { ATTR_SERVICE_NAME } from '@opentelemetry/semantic-conventions'
import { trace } from '@opentelemetry/api'

// Parse CLI args (same interface as Go client)
const argv = parseArgs(process.argv.slice(2), {
  string: ['role', 'transport', 'peer-dir', 'peers', 'otlp-endpoint', 'trace-file'],
  default: {
    role: 'client',
    transport: 'both',
    port: 4001,
    'peer-dir': '',
    peers: '',
    'otlp-endpoint': '',
    'trace-file': '',
    'obs-addr-thresh': 0,
  },
})

const role = argv['role'] as string
const transport = argv['transport'] as string
const port = Number(argv['port'])
const peerDir = argv['peer-dir'] as string
const peersStr = argv['peers'] as string
const otlpEndpoint = argv['otlp-endpoint'] as string
const traceFile = argv['trace-file'] as string

// JSONL span format compatible with analyze.py
interface JsonlSpan {
  Name: string
  SpanContext: { TraceID: string }
  Attributes: Array<{
    Key: string
    Value: { Type: string; Value: unknown }
  }>
}

// Generate a fixed trace ID for this session
const sessionTraceId = Array.from({ length: 32 }, () =>
  Math.floor(Math.random() * 16).toString(16)
).join('')

let traceFileStream: fs.WriteStream | null = null

function writeJsonlSpan(name: string, attrs: Record<string, unknown>): void {
  if (!traceFileStream) return

  const jsonlAttrs = Object.entries(attrs).map(([key, value]) => {
    if (Array.isArray(value)) {
      return { Key: key, Value: { Type: 'STRINGSLICE', Value: value.map(String) } }
    }
    if (typeof value === 'number') {
      return { Key: key, Value: { Type: 'INT64', Value: value } }
    }
    return { Key: key, Value: { Type: 'STRING', Value: String(value) } }
  })

  const span: JsonlSpan = {
    Name: name,
    SpanContext: { TraceID: sessionTraceId },
    Attributes: jsonlAttrs,
  }

  traceFileStream.write(JSON.stringify(span) + '\n')
}

const startTime = Date.now()

function emitSpan(name: string, attrs: Record<string, unknown>): void {
  const elapsed_ms = Date.now() - startTime
  const fullAttrs = { elapsed_ms, ...attrs }

  // OTLP span
  const tracer = trace.getTracer('autonat-testbed')
  const span = tracer.startSpan(name)
  for (const [key, value] of Object.entries(fullAttrs)) {
    if (Array.isArray(value)) {
      span.setAttribute(key, value.map(String))
    } else if (typeof value === 'number') {
      span.setAttribute(key, value)
    } else {
      span.setAttribute(key, String(value))
    }
  }
  span.end()

  // JSONL file
  writeJsonlSpan(name, fullAttrs)
}

async function main(): Promise<void> {
  // Initialize OpenTelemetry SDK
  let sdk: NodeSDK | undefined
  if (otlpEndpoint) {
    const exporter = new OTLPTraceExporter({
      url: `${otlpEndpoint}/v1/traces`,
    })
    sdk = new NodeSDK({
      resource: new Resource({
        [ATTR_SERVICE_NAME]: 'autonat-testbed',
      }),
      traceExporter: exporter,
    })
    sdk.start()
    console.error(`OTEL tracing enabled, exporting to ${otlpEndpoint}`)
  }

  // Initialize JSONL file writer
  if (traceFile) {
    traceFileStream = fs.createWriteStream(traceFile)
    console.error(`OTEL tracing enabled, writing to ${traceFile}`)
  }

  // Build listen addresses
  const listenAddrs: string[] = []
  if (transport === 'tcp' || transport === 'both') {
    listenAddrs.push(`/ip4/0.0.0.0/tcp/${port}`)
  }
  if (transport === 'quic' || transport === 'both') {
    listenAddrs.push(`/ip4/0.0.0.0/udp/${port}/quic-v1`)
  }

  // Create libp2p node
  const transports: any[] = [tcp()]
  // QUIC transport is optional — try to load it
  if (transport === 'quic' || transport === 'both') {
    try {
      const quicMod = await import('@chainsafe/libp2p-quic')
      transports.push(quicMod.quic())
    } catch {
      console.error('Warning: QUIC transport not available, using TCP only')
      const quicIdx = listenAddrs.findIndex(a => a.includes('quic'))
      if (quicIdx >= 0) listenAddrs.splice(quicIdx, 1)
    }
  }

  const node = await createLibp2p({
    addresses: { listen: listenAddrs },
    transports,
    connectionEncrypters: [noise()],
    streamMuxers: [yamux()],
    services: {
      identify: identify(),
      autonat: autoNATv2(),
    },
    connectionManager: {
      maxConnections: 100,
    },
  })

  const localPeerId = node.peerId.toString()
  console.error(`Node started: ${localPeerId} (role=${role})`)
  for (const ma of node.getMultiaddrs()) {
    console.error(`  Listening on: ${ma.toString()}`)
  }

  emitSpan('started', {
    peer_id: localPeerId,
    message: `role=${role} transport=${transport}`,
  })

  // Track reachability state
  const reachableAddrs = new Set<string>()
  const unreachableAddrs = new Set<string>()

  // Listen for reachability events
  // js-libp2p emits 'self:peer:update' when peer info changes
  node.addEventListener('self:peer:update', (evt: any) => {
    const addresses = node.getMultiaddrs().map(ma => ma.toString())
    console.error(`Peer update: ${addresses.length} addresses`)

    // Check which addresses are confirmed reachable via AutoNAT
    const currentAddrs = node.getMultiaddrs()
    for (const addr of currentAddrs) {
      const addrStr = addr.toString()
      if (!reachableAddrs.has(addrStr)) {
        reachableAddrs.add(addrStr)
        unreachableAddrs.delete(addrStr)
      }
    }

    if (reachableAddrs.size > 0 || unreachableAddrs.size > 0) {
      emitSpan('reachable_addrs_changed', {
        reachable: Array.from(reachableAddrs),
        unreachable: Array.from(unreachableAddrs),
        unknown: [],
      })
    }
  })

  // Connect to peers from --peer-dir or --peers
  if (peerDir) {
    await connectFromDir(node, peerDir)
  } else if (peersStr) {
    for (const addrStr of peersStr.split(',')) {
      const trimmed = addrStr.trim()
      if (!trimmed) continue
      try {
        const ma = multiaddr(trimmed)
        const parts = trimmed.split('/p2p/')
        if (parts.length >= 2) {
          const peerId = peerIdFromString(parts[parts.length - 1])
          await node.dial(ma)
          console.error(`Connected to ${peerId}`)
          emitSpan('connected', { peer_id: peerId.toString() })
        }
      } catch (e: any) {
        console.error(`Failed to connect to ${trimmed}: ${e.message}`)
        emitSpan('connect_failed', { message: e.message })
      }
    }
  }

  // Handle shutdown
  const shutdown = async () => {
    emitSpan('shutdown', { message: 'received signal, shutting down' })
    console.error('Shutting down...')

    if (traceFileStream) {
      traceFileStream.end()
    }

    if (sdk) {
      await sdk.shutdown()
    }

    await node.stop()
    process.exit(0)
  }

  process.on('SIGINT', shutdown)
  process.on('SIGTERM', shutdown)

  // Keep running
  await new Promise(() => {})
}

async function connectFromDir(node: any, dir: string): Promise<void> {
  // Wait for addr files to appear
  const maxWait = 30_000
  const start = Date.now()
  while (true) {
    try {
      const entries = fs.readdirSync(dir)
      if (entries.length > 0) break
    } catch {
      // dir doesn't exist yet
    }
    if (Date.now() - start > maxWait) {
      console.error(`Timeout waiting for peer addr files in ${dir}`)
      return
    }
    await new Promise(r => setTimeout(r, 1000))
  }

  // Small delay for all servers to write files
  await new Promise(r => setTimeout(r, 3000))

  emitSpan('peer_discovery_start', {
    message: `reading server addresses from ${dir}`,
  })

  const entries = fs.readdirSync(dir)
  const peerAddrs = new Map<string, string[]>()

  for (const entry of entries) {
    const filePath = path.join(dir, entry)
    const stat = fs.statSync(filePath)
    if (!stat.isFile()) continue

    const data = fs.readFileSync(filePath, 'utf-8')
    for (const line of data.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      const parts = trimmed.split('/p2p/')
      if (parts.length >= 2) {
        const peerId = parts[parts.length - 1]
        if (!peerAddrs.has(peerId)) {
          peerAddrs.set(peerId, [])
        }
        peerAddrs.get(peerId)!.push(trimmed)
      }
    }
  }

  for (const [peerId, addrs] of peerAddrs) {
    for (const addrStr of addrs) {
      try {
        const ma = multiaddr(addrStr)
        await node.dial(ma)
        console.error(`Connected to server ${peerId.substring(0, 16)}...`)
        emitSpan('connected', { peer_id: peerId })
        break // Connected via one addr, skip the rest for this peer
      } catch (e: any) {
        console.error(`Failed to dial ${addrStr}: ${e.message}`)
      }
    }
  }

  emitSpan('peer_discovery_done', {
    message: `connected to servers from ${dir}`,
  })
}

main().catch(err => {
  console.error('Fatal error:', err)
  process.exit(1)
})
