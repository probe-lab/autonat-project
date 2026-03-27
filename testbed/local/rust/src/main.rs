use clap::Parser;
use futures::StreamExt;
use libp2p::{
    autonat, identify, kad,
    multiaddr::Protocol,
    noise,
    swarm::SwarmEvent,
    tcp, upnp, yamux, Multiaddr, PeerId, SwarmBuilder,
};
use serde::Serialize;
use std::collections::HashSet;
use std::fs;
use std::io::{BufWriter, Write};
use std::sync::Mutex;
use std::time::Instant;

#[derive(Parser, Debug)]
#[command(name = "autonat-local-rust")]
struct Args {
    #[arg(long, default_value = "both")]
    transport: String,

    #[arg(long, default_value = "4001")]
    port: u16,

    #[arg(long)]
    trace_file: Option<String>,

    #[arg(long, default_value = "false")]
    bootstrap: bool,

    #[arg(long)]
    peers: Option<String>,
}

// IPFS bootstrap peers
const BOOTSTRAP_PEERS: &[&str] = &[
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmNnooDu7bfjPFoTZYxMNLWUQJyrVwtbZg5gBMjTezGAJN",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmQCU2EcMqAqQPR2i9bChDtGNJchTbq5TbXJJ16u19uLTa",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmbLHAnMoJPWSCR5Zhtx6BHJX9KiKNN6tpvbUcqanj75Nb",
    "/dnsaddr/bootstrap.libp2p.io/p2p/QmcZf59bWwK5XFi76CZX8cbJ4BhTzzA3gU1ZjYZcYW3dwt",
];

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
                .as_nanos() as u64,
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

fn emit_span(
    name: &str,
    attrs: Vec<JsonlAttr>,
    jsonl_writer: &Option<Mutex<JsonlWriter>>,
) {
    if let Some(writer) = jsonl_writer {
        if let Ok(mut w) = writer.lock() {
            w.write_span(name, attrs);
        }
    }
}

fn peer_id_from_multiaddr(addr: &Multiaddr) -> Option<PeerId> {
    addr.iter().find_map(|p| {
        if let Protocol::P2p(id) = p {
            Some(id)
        } else {
            None
        }
    })
}

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

    let jsonl_writer: Option<Mutex<JsonlWriter>> = args
        .trace_file
        .as_ref()
        .and_then(|path| JsonlWriter::new(path).ok())
        .map(Mutex::new);

    let start_time = Instant::now();

    let mut swarm = SwarmBuilder::with_new_identity()
        .with_tokio()
        .with_tcp(
            tcp::Config::default(),
            noise::Config::new,
            yamux::Config::default,
        )?
        .with_quic()
        .with_dns()?
        .with_behaviour(|key| {
            let identify_config =
                identify::Config::new("/ipfs/id/1.0.0".to_string(), key.public());

            let mut kad_config = kad::Config::default();
            kad_config.set_protocol_names(vec![
                libp2p::StreamProtocol::new("/ipfs/kad/1.0.0"),
            ]);

            Ok(NodeBehaviour {
                identify: identify::Behaviour::new(identify_config),
                autonat_client: autonat::v2::client::Behaviour::new(
                    rand::rngs::OsRng,
                    autonat::v2::client::Config::default(),
                ),
                autonat_server: autonat::v2::server::Behaviour::new(rand::rngs::OsRng),
                upnp: upnp::tokio::Behaviour::default(),
                kademlia: kad::Behaviour::new(key.public().to_peer_id(), kad_config),
            })
        })?
        .with_swarm_config(|cfg| {
            cfg.with_idle_connection_timeout(std::time::Duration::from_secs(120))
        })
        .build();

    // Build listen addresses
    let listen_addrs = build_listen_addrs("0.0.0.0", args.port, &args.transport);
    for addr_str in &listen_addrs {
        let addr: Multiaddr = addr_str.parse()?;
        swarm.listen_on(addr)?;
    }

    let local_peer_id = *swarm.local_peer_id();
    eprintln!("Node started: {} (transport={})", local_peer_id, args.transport);
    eprintln!("UPnP: enabled");

    // Wait for listeners
    let expected_listeners = listen_addrs.len();
    let mut ready_listeners = 0;
    while ready_listeners < expected_listeners {
        if let Some(event) = swarm.next().await {
            if let SwarmEvent::NewListenAddr { address, .. } = event {
                eprintln!("Listening on {}/p2p/{}", address, local_peer_id);
                let addr_str = address.to_string();
                if !addr_str.contains("/127.0.0.1/") {
                    ready_listeners += 1;
                }
            }
        }
    }

    let elapsed_ms = start_time.elapsed().as_millis() as i64;
    emit_span(
        "started",
        vec![
            attr_int("elapsed_ms", elapsed_ms),
            attr_string("peer_id", &local_peer_id.to_string()),
            attr_string("message", &format!("transport={} upnp=true", args.transport)),
        ],
        &jsonl_writer,
    );

    // Bootstrap to DHT
    if args.bootstrap {
        eprintln!("Bootstrapping to IPFS DHT...");
        for addr_str in BOOTSTRAP_PEERS {
            if let Ok(addr) = addr_str.parse::<Multiaddr>() {
                if let Some(pid) = peer_id_from_multiaddr(&addr) {
                    swarm.behaviour_mut().kademlia.add_address(&pid, strip_p2p(&addr));
                    let dial_opts = libp2p::swarm::dial_opts::DialOpts::peer_id(pid)
                        .addresses(vec![strip_p2p(&addr)])
                        .build();
                    if let Err(e) = swarm.dial(dial_opts) {
                        eprintln!("Failed to dial bootstrap {}: {}", pid, e);
                    }
                }
            }
        }
        if let Err(e) = swarm.behaviour_mut().kademlia.bootstrap() {
            eprintln!("Kademlia bootstrap failed: {}", e);
        }
    }

    // Connect to explicit peers
    if let Some(ref peers_str) = args.peers {
        for addr_str in peers_str.split(',') {
            let addr_str = addr_str.trim();
            if addr_str.is_empty() { continue; }
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

    let mut reachable_addrs: HashSet<String> = HashSet::new();
    let mut unreachable_addrs: HashSet<String> = HashSet::new();

    // Event loop
    loop {
        tokio::select! {
            event = swarm.select_next_some() => {
                match event {
                    SwarmEvent::Behaviour(NodeBehaviourEvent::AutonatClient(ev)) => {
                        let elapsed_ms = start_time.elapsed().as_millis() as i64;
                        let addr_str = ev.tested_addr.to_string();

                        match ev.result {
                            Ok(()) => {
                                eprintln!("[{:>6}ms] AutoNAT v2: {} REACHABLE (server={})",
                                    elapsed_ms, addr_str, ev.server);
                                reachable_addrs.insert(addr_str.clone());
                                unreachable_addrs.remove(&addr_str);
                            }
                            Err(ref e) => {
                                eprintln!("[{:>6}ms] AutoNAT v2: {} UNREACHABLE (server={}, error={:?})",
                                    elapsed_ms, addr_str, ev.server, e);
                                unreachable_addrs.insert(addr_str.clone());
                                reachable_addrs.remove(&addr_str);
                            }
                        }

                        let reachable_vec: Vec<String> = reachable_addrs.iter().cloned().collect();
                        let unreachable_vec: Vec<String> = unreachable_addrs.iter().cloned().collect();

                        emit_span(
                            "reachable_addrs_changed",
                            vec![
                                attr_int("elapsed_ms", elapsed_ms),
                                attr_string_slice("reachable", &reachable_vec),
                                attr_string_slice("unreachable", &unreachable_vec),
                                attr_string_slice("unknown", &[]),
                            ],
                            &jsonl_writer,
                        );
                    }

                    SwarmEvent::Behaviour(NodeBehaviourEvent::Upnp(upnp::Event::NewExternalAddr(addr))) => {
                        let elapsed_ms = start_time.elapsed().as_millis() as i64;
                        eprintln!("[{:>6}ms] UPnP: new external address {}", elapsed_ms, addr);
                    }

                    SwarmEvent::Behaviour(NodeBehaviourEvent::Upnp(upnp::Event::GatewayNotFound)) => {
                        eprintln!("UPnP: gateway not found");
                    }

                    SwarmEvent::Behaviour(NodeBehaviourEvent::Upnp(upnp::Event::NonRoutableGateway)) => {
                        eprintln!("UPnP: non-routable gateway");
                    }

                    SwarmEvent::ConnectionEstablished { peer_id, .. } => {
                        let elapsed_ms = start_time.elapsed().as_millis() as i64;
                        eprintln!("[{:>6}ms] Connected to {}", elapsed_ms, peer_id);
                        emit_span(
                            "connected",
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
                        attr_int("elapsed_ms", elapsed_ms),
                        attr_string("message", "received signal, shutting down"),
                    ],
                    &jsonl_writer,
                );
                break;
            }
        }
    }

    Ok(())
}

#[derive(libp2p::swarm::NetworkBehaviour)]
struct NodeBehaviour {
    identify: identify::Behaviour,
    autonat_client: autonat::v2::client::Behaviour,
    autonat_server: autonat::v2::server::Behaviour,
    upnp: upnp::tokio::Behaviour,
    kademlia: kad::Behaviour<kad::store::MemoryStore>,
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
