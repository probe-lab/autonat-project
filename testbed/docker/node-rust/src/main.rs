use clap::Parser;
use futures::StreamExt;
use libp2p::{
    autonat,
    identify,
    multiaddr::Protocol,
    noise,
    swarm::SwarmEvent,
    tcp, yamux, Multiaddr, PeerId, SwarmBuilder,
};
use opentelemetry::{global, trace::Tracer, KeyValue};
use opentelemetry_otlp::WithExportConfig;
use opentelemetry_sdk::trace::TracerProvider;
use opentelemetry_sdk::Resource;
use serde::Serialize;
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufWriter, Write};
use std::sync::Mutex;
use std::time::Instant;

#[derive(Parser, Debug)]
#[command(name = "autonat-node-rust")]
struct Args {
    #[arg(long, default_value = "client")]
    role: String,

    #[arg(long, default_value = "both")]
    transport: String,

    #[arg(long, default_value = "4001")]
    port: u16,

    #[arg(long)]
    peer_dir: Option<String>,

    #[arg(long)]
    peers: Option<String>,

    #[arg(long)]
    otlp_endpoint: Option<String>,

    #[arg(long)]
    trace_file: Option<String>,

    #[arg(long, default_value = "0")]
    obs_addr_thresh: u32,
}

// ---------------------------------------------------------------------------
// JSONL span format compatible with analyze.py
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct JsonlSpan {
    #[serde(rename = "Name")]
    name: String,
    #[serde(rename = "SpanContext")]
    span_context: SpanContext,
    #[serde(rename = "Attributes")]
    attributes: Vec<JsonlAttr>,
}

#[derive(Serialize)]
struct SpanContext {
    #[serde(rename = "TraceID")]
    trace_id: String,
}

#[derive(Serialize)]
struct JsonlAttr {
    #[serde(rename = "Key")]
    key: String,
    #[serde(rename = "Value")]
    value: JsonlAttrValue,
}

#[derive(Serialize)]
struct JsonlAttrValue {
    #[serde(rename = "Type")]
    r#type: String,
    #[serde(rename = "Value")]
    value: serde_json::Value,
}

fn attr_string(key: &str, val: &str) -> JsonlAttr {
    JsonlAttr {
        key: key.to_string(),
        value: JsonlAttrValue {
            r#type: "STRING".to_string(),
            value: serde_json::Value::String(val.to_string()),
        },
    }
}

fn attr_int(key: &str, val: i64) -> JsonlAttr {
    JsonlAttr {
        key: key.to_string(),
        value: JsonlAttrValue {
            r#type: "INT64".to_string(),
            value: serde_json::Value::Number(serde_json::Number::from(val)),
        },
    }
}

fn attr_string_slice(key: &str, vals: &[String]) -> JsonlAttr {
    JsonlAttr {
        key: key.to_string(),
        value: JsonlAttrValue {
            r#type: "STRINGSLICE".to_string(),
            value: serde_json::Value::Array(
                vals.iter()
                    .map(|s| serde_json::Value::String(s.clone()))
                    .collect(),
            ),
        },
    }
}

// ---------------------------------------------------------------------------
// File-based JSONL writer for trace compatibility with analyze.py
// ---------------------------------------------------------------------------

struct JsonlWriter {
    writer: BufWriter<fs::File>,
    trace_id: String,
}

impl JsonlWriter {
    fn new(path: &str) -> std::io::Result<Self> {
        let file = fs::File::create(path)?;
        let trace_id = format!(
            "{:016x}{:016x}",
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
                as u64,
            std::process::id() as u64
        );
        Ok(Self {
            writer: BufWriter::new(file),
            trace_id,
        })
    }

    fn write_span(&mut self, name: &str, attrs: Vec<JsonlAttr>) {
        let span = JsonlSpan {
            name: name.to_string(),
            span_context: SpanContext {
                trace_id: self.trace_id.clone(),
            },
            attributes: attrs,
        };
        if let Ok(json) = serde_json::to_string(&span) {
            let _ = writeln!(self.writer, "{}", json);
            let _ = self.writer.flush();
        }
    }
}

/// Emit a span via both OTLP (if configured) and JSONL file (if configured).
fn emit_span(
    name: &str,
    otel_attrs: Vec<KeyValue>,
    jsonl_attrs: Vec<JsonlAttr>,
    jsonl_writer: &Option<Mutex<JsonlWriter>>,
) {
    // OTLP span
    let tracer = global::tracer("autonat-testbed");
    let span = tracer
        .span_builder(name.to_string())
        .with_attributes(otel_attrs)
        .start(&tracer);
    drop(span); // end immediately (short-lived span)

    // JSONL file
    if let Some(writer) = jsonl_writer {
        if let Ok(mut w) = writer.lock() {
            w.write_span(name, jsonl_attrs);
        }
    }
}

/// Extract PeerId from a multiaddr containing /p2p/<peer-id>.
fn peer_id_from_multiaddr(addr: &Multiaddr) -> Option<PeerId> {
    addr.iter().find_map(|p| {
        if let Protocol::P2p(id) = p {
            Some(id)
        } else {
            None
        }
    })
}

/// Strip /p2p/<peer-id> component from a multiaddr.
fn strip_p2p(addr: &Multiaddr) -> Multiaddr {
    addr.iter()
        .filter(|p| !matches!(p, Protocol::P2p(_)))
        .collect()
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| "info".into()),
        )
        .init();

    let args = Args::parse();

    // Initialize OTLP tracing
    let _provider = if let Some(ref endpoint) = args.otlp_endpoint {
        let exporter = opentelemetry_otlp::SpanExporter::builder()
            .with_http()
            .with_endpoint(format!("{}/v1/traces", endpoint))
            .build()?;

        let resource = Resource::new(vec![KeyValue::new("service.name", "autonat-testbed")]);

        let provider = TracerProvider::builder()
            .with_batch_exporter(exporter, opentelemetry_sdk::runtime::Tokio)
            .with_resource(resource)
            .build();

        global::set_tracer_provider(provider.clone());
        Some(provider)
    } else {
        None
    };

    // Initialize JSONL file writer
    let jsonl_writer: Option<Mutex<JsonlWriter>> = args
        .trace_file
        .as_ref()
        .and_then(|path| JsonlWriter::new(path).ok())
        .map(Mutex::new);

    let start_time = Instant::now();

    // Build swarm with TCP + QUIC + Noise + Yamux + Identify + AutoNAT v2
    let mut swarm = SwarmBuilder::with_new_identity()
        .with_tokio()
        .with_tcp(
            tcp::Config::default(),
            noise::Config::new,
            yamux::Config::default,
        )?
        .with_quic()
        .with_behaviour(|key| {
            let identify_config =
                identify::Config::new("/ipfs/id/1.0.0".to_string(), key.public());

            Ok(NodeBehaviour {
                identify: identify::Behaviour::new(identify_config),
                autonat_client: autonat::v2::client::Behaviour::new(
                    rand::rngs::OsRng,
                    autonat::v2::client::Config::default(),
                ),
                autonat_server: autonat::v2::server::Behaviour::new(rand::rngs::OsRng),
            })
        })?
        .with_swarm_config(|cfg| {
            cfg.with_idle_connection_timeout(std::time::Duration::from_secs(120))
        })
        .build();

    // Build listen addresses based on transport flag
    let listen_addrs = build_listen_addrs("0.0.0.0", args.port, &args.transport);
    for addr_str in &listen_addrs {
        let addr: Multiaddr = addr_str.parse()?;
        swarm.listen_on(addr)?;
    }

    let local_peer_id = *swarm.local_peer_id();
    eprintln!("Node started: {} (role={})", local_peer_id, args.role);

    // Emit started span
    let elapsed_ms = start_time.elapsed().as_millis() as i64;
    emit_span(
        "started",
        vec![
            KeyValue::new("elapsed_ms", elapsed_ms),
            KeyValue::new("peer_id", local_peer_id.to_string()),
            KeyValue::new(
                "message",
                format!("role={} transport={}", args.role, args.transport),
            ),
        ],
        vec![
            attr_int("elapsed_ms", elapsed_ms),
            attr_string("peer_id", &local_peer_id.to_string()),
            attr_string(
                "message",
                &format!("role={} transport={}", args.role, args.transport),
            ),
        ],
        &jsonl_writer,
    );

    // Wait for all listeners to be ready before dialing peers.
    // This ensures TCP port reuse can find the listen address.
    let expected_listeners = listen_addrs.len();
    let mut ready_listeners = 0;
    eprintln!("Waiting for {} listeners to be ready...", expected_listeners);
    while ready_listeners < expected_listeners {
        if let Some(event) = swarm.next().await {
            match event {
                SwarmEvent::NewListenAddr { address, .. } => {
                    eprintln!("Listening on {}/p2p/{}", address, local_peer_id);
                    let addr_str = address.to_string();
                    if !addr_str.contains("/127.0.0.1/") {
                        swarm.add_external_address(address.clone());
                        ready_listeners += 1;
                    }
                }
                _ => {}
            }
        }
    }
    eprintln!("All {} listeners ready, connecting to peers...", ready_listeners);

    // Connect to peers from --peer-dir or --peers
    if let Some(ref peer_dir) = args.peer_dir {
        connect_from_dir(&mut swarm, peer_dir, start_time, &jsonl_writer).await;
    } else if let Some(ref peers_str) = args.peers {
        for addr_str in peers_str.split(',') {
            let addr_str = addr_str.trim();
            if addr_str.is_empty() {
                continue;
            }
            if let Ok(addr) = addr_str.parse::<Multiaddr>() {
                if let Some(pid) = peer_id_from_multiaddr(&addr) {
                    let dial_opts = libp2p::swarm::dial_opts::DialOpts::peer_id(pid)
                        .addresses(vec![strip_p2p(&addr)])
                        .build();
                    if let Err(e) = swarm.dial(dial_opts) {
                        eprintln!("Failed to dial {}: {}", pid, e);
                    }
                }
            }
        }
    }

    // Track reachability state across autonat v2 events
    let mut reachable_addrs: HashSet<String> = HashSet::new();
    let mut unreachable_addrs: HashSet<String> = HashSet::new();

    // Event loop
    loop {
        tokio::select! {
            event = swarm.select_next_some() => {
                match event {
                    SwarmEvent::Behaviour(NodeBehaviourEvent::AutonatClient(ev)) => {
                        let elapsed_ms = start_time.elapsed().as_millis() as i64;
                        handle_autonat_event(
                            ev,
                            elapsed_ms,
                            &mut reachable_addrs,
                            &mut unreachable_addrs,
                            &jsonl_writer,
                        );
                    }

                    SwarmEvent::Behaviour(NodeBehaviourEvent::AutonatServer(_)) => {}

                    SwarmEvent::ConnectionEstablished { peer_id, endpoint, .. } => {
                        let elapsed_ms = start_time.elapsed().as_millis() as i64;
                        eprintln!("Connected to {} endpoint={:?}", peer_id, endpoint);
                        emit_span(
                            "connected",
                            vec![
                                KeyValue::new("elapsed_ms", elapsed_ms),
                                KeyValue::new("peer_id", peer_id.to_string()),
                            ],
                            vec![
                                attr_int("elapsed_ms", elapsed_ms),
                                attr_string("peer_id", &peer_id.to_string()),
                            ],
                            &jsonl_writer,
                        );
                    }

                    SwarmEvent::NewExternalAddrCandidate { address } => {
                        eprintln!("NewExternalAddrCandidate: {}", address);
                    }

                    SwarmEvent::ExternalAddrConfirmed { address } => {
                        eprintln!("ExternalAddrConfirmed: {}", address);
                    }

                    SwarmEvent::OutgoingConnectionError { peer_id, error, .. } => {
                        eprintln!("OutgoingConnectionError: peer={:?} error={}", peer_id, error);
                    }

                    SwarmEvent::IncomingConnection { local_addr, send_back_addr, .. } => {
                        eprintln!("IncomingConnection: local={} remote={}", local_addr, send_back_addr);
                    }

                    SwarmEvent::NewListenAddr { address, .. } => {
                        eprintln!("Listening on {}/p2p/{}", address, local_peer_id);
                    }

                    _ => {}
                }
            }

            _ = tokio::signal::ctrl_c() => {
                let elapsed_ms = start_time.elapsed().as_millis() as i64;
                eprintln!("Shutting down...");
                emit_span(
                    "shutdown",
                    vec![
                        KeyValue::new("elapsed_ms", elapsed_ms),
                        KeyValue::new("message", "received signal, shutting down"),
                    ],
                    vec![
                        attr_int("elapsed_ms", elapsed_ms),
                        attr_string("message", "received signal, shutting down"),
                    ],
                    &jsonl_writer,
                );
                break;
            }
        }
    }

    // Flush OTLP
    if let Some(provider) = _provider {
        global::shutdown_tracer_provider();
        drop(provider);
    }

    Ok(())
}

/// Handle autonat v2 client events and emit reachable_addrs_changed spans.
///
/// The autonat::v2::client::Event struct reports per-address test results
/// with fields: tested_addr, bytes_sent, server, result.
fn handle_autonat_event(
    ev: autonat::v2::client::Event,
    elapsed_ms: i64,
    reachable_addrs: &mut HashSet<String>,
    unreachable_addrs: &mut HashSet<String>,
    jsonl_writer: &Option<Mutex<JsonlWriter>>,
) {
    let addr_str = ev.tested_addr.to_string();

    match ev.result {
        Ok(()) => {
            eprintln!(
                "AutoNAT v2: {} REACHABLE (server={}, bytes_sent={})",
                addr_str, ev.server, ev.bytes_sent
            );
            reachable_addrs.insert(addr_str.clone());
            unreachable_addrs.remove(&addr_str);
        }
        Err(ref e) => {
            eprintln!(
                "AutoNAT v2: {} UNREACHABLE (server={}, error={:?})",
                addr_str, ev.server, e
            );
            unreachable_addrs.insert(addr_str.clone());
            reachable_addrs.remove(&addr_str);
        }
    }

    let reachable_vec: Vec<String> = reachable_addrs.iter().cloned().collect();
    let unreachable_vec: Vec<String> = unreachable_addrs.iter().cloned().collect();

    emit_span(
        "reachable_addrs_changed",
        vec![
            KeyValue::new("elapsed_ms", elapsed_ms),
            KeyValue::new("reachable", format!("{:?}", reachable_vec)),
            KeyValue::new("unreachable", format!("{:?}", unreachable_vec)),
            KeyValue::new("unknown", "[]".to_string()),
        ],
        vec![
            attr_int("elapsed_ms", elapsed_ms),
            attr_string_slice("reachable", &reachable_vec),
            attr_string_slice("unreachable", &unreachable_vec),
            attr_string_slice("unknown", &[]),
        ],
        jsonl_writer,
    );
}

#[derive(libp2p::swarm::NetworkBehaviour)]
struct NodeBehaviour {
    identify: identify::Behaviour,
    autonat_client: autonat::v2::client::Behaviour,
    autonat_server: autonat::v2::server::Behaviour,
}

fn build_listen_addrs(ip: &str, port: u16, transport: &str) -> Vec<String> {
    match transport {
        "tcp" => vec![format!("/ip4/{}/tcp/{}", ip, port)],
        "quic" => vec![format!("/ip4/{}/udp/{}/quic-v1", ip, port)],
        "both" => vec![
            format!("/ip4/{}/tcp/{}", ip, port),
            format!("/ip4/{}/udp/{}/quic-v1", ip, port),
        ],
        _ => {
            eprintln!("Unknown transport: {} (use tcp, quic, or both)", transport);
            std::process::exit(1);
        }
    }
}

async fn connect_from_dir(
    swarm: &mut libp2p::Swarm<NodeBehaviour>,
    dir: &str,
    start_time: Instant,
    jsonl_writer: &Option<Mutex<JsonlWriter>>,
) {
    // Wait for addr files to appear
    let max_wait = std::time::Duration::from_secs(30);
    let start = Instant::now();
    loop {
        if let Ok(entries) = fs::read_dir(dir) {
            if entries.count() > 0 {
                break;
            }
        }
        if start.elapsed() > max_wait {
            eprintln!("Timeout waiting for peer addr files in {}", dir);
            return;
        }
        tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    }

    // Small delay for all servers to write files
    tokio::time::sleep(std::time::Duration::from_secs(3)).await;

    let elapsed_ms = start_time.elapsed().as_millis() as i64;
    emit_span(
        "peer_discovery_start",
        vec![
            KeyValue::new("elapsed_ms", elapsed_ms),
            KeyValue::new("message", format!("reading server addresses from {}", dir)),
        ],
        vec![
            attr_int("elapsed_ms", elapsed_ms),
            attr_string(
                "message",
                &format!("reading server addresses from {}", dir),
            ),
        ],
        jsonl_writer,
    );

    let entries = match fs::read_dir(dir) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("Failed to read peer dir: {}", e);
            return;
        }
    };

    // Collect addresses grouped by peer ID
    let mut peer_addrs: HashMap<PeerId, Vec<Multiaddr>> = HashMap::new();
    for entry in entries.flatten() {
        if entry.file_type().map_or(true, |ft| !ft.is_file()) {
            continue;
        }
        if let Ok(data) = fs::read_to_string(entry.path()) {
            for line in data.lines() {
                let line = line.trim();
                if line.is_empty() {
                    continue;
                }
                if let Ok(addr) = line.parse::<Multiaddr>() {
                    if let Some(pid) = peer_id_from_multiaddr(&addr) {
                        peer_addrs.entry(pid).or_default().push(strip_p2p(&addr));
                    }
                }
            }
        }
    }

    // Connect to each peer with all their addresses
    for (pid, addrs) in &peer_addrs {
        let dial_opts = libp2p::swarm::dial_opts::DialOpts::peer_id(*pid)
            .addresses(addrs.clone())
            .build();
        match swarm.dial(dial_opts) {
            Ok(()) => eprintln!("Dialing server {}", pid),
            Err(e) => {
                eprintln!("Failed to dial {}: {}", pid, e);
                let elapsed_ms = start_time.elapsed().as_millis() as i64;
                emit_span(
                    "connect_failed",
                    vec![
                        KeyValue::new("elapsed_ms", elapsed_ms),
                        KeyValue::new("peer_id", pid.to_string()),
                        KeyValue::new("message", e.to_string()),
                    ],
                    vec![
                        attr_int("elapsed_ms", elapsed_ms),
                        attr_string("peer_id", &pid.to_string()),
                        attr_string("message", &e.to_string()),
                    ],
                    jsonl_writer,
                );
            }
        }
    }

    let elapsed_ms = start_time.elapsed().as_millis() as i64;
    emit_span(
        "peer_discovery_done",
        vec![
            KeyValue::new("elapsed_ms", elapsed_ms),
            KeyValue::new(
                "message",
                format!("connected to servers from {}", dir),
            ),
        ],
        vec![
            attr_int("elapsed_ms", elapsed_ms),
            attr_string("message", &format!("connected to servers from {}", dir)),
        ],
        jsonl_writer,
    );
}
