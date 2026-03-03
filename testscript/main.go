package main

import (
	"context"
	"fmt"

	"github.com/libp2p/go-libp2p"
	kaddht "github.com/libp2p/go-libp2p-kad-dht"
	"github.com/libp2p/go-libp2p/core/event"
	"github.com/libp2p/go-libp2p/core/host"
	"github.com/libp2p/go-libp2p/core/routing"
)

func main() {
	var dht routing.Routing
	h, err := libp2p.New(
		libp2p.EnableAutoNATv2(),
		libp2p.ListenAddrStrings(
			"/ip4/0.0.0.0/tcp/4003",
			"/ip4/0.0.0.0/udp/4003/quic-v1",
			"/ip4/0.0.0.0/udp/4003/quic-v1/webtransport",
			"/ip4/0.0.0.0/udp/4003/webrtc-direct",
			"/ip6/::/tcp/4003",
			"/ip6/::/udp/4003/quic-v1",
			"/ip6/::/udp/4003/quic-v1/webtransport",
			"/ip6/::/udp/4003/webrtc-direct",
		),
		libp2p.Routing(func(host host.Host) (routing.PeerRouting, error) {
			var err error
			dht, err = kaddht.New(context.Background(), host)
			return dht, err
		}),
	)
	if err != nil {
		panic(err)
	}

	fmt.Println("Started libp2p host")

	go func() {
		for _, addrInfo := range kaddht.GetDefaultBootstrapPeerAddrInfos() {
			fmt.Println("Connecting to bootstrap peer:", addrInfo.ID)
			if err := h.Connect(context.Background(), addrInfo); err != nil {
				fmt.Printf("Failed to connect to bootstrap peer %s: %v\n", addrInfo.ID, err)
			}
		}
	}()

	sub, err := h.EventBus().Subscribe([]any{
		new(event.EvtHostReachableAddrsChanged),
		new(event.EvtLocalReachabilityChanged),
		new(event.EvtNATDeviceTypeChanged),
		new(event.EvtLocalAddressesUpdated),
	})
	if err != nil {
		panic(err)
	}
	defer sub.Close()

	for evt := range sub.Out() {
		switch tevt := evt.(type) {
		case event.EvtHostReachableAddrsChanged:
			fmt.Println("Reachable addresses changed:")
			for _, addr := range tevt.Reachable {
				fmt.Println("\tReachable:", addr)
			}
			for _, addr := range tevt.Unreachable {
				fmt.Println("\tUnreachable:", addr)
			}
			for _, addr := range tevt.Unknown {
				fmt.Println("\tUnknown:", addr)
			}
		case event.EvtLocalReachabilityChanged:
			fmt.Println("Local reachability changed:", tevt.Reachability.String())
		case event.EvtNATDeviceTypeChanged:
			fmt.Printf("NAT Device Type (%s): %s\n", tevt.TransportProtocol, tevt.NatDeviceType)
		case event.EvtLocalAddressesUpdated:
			fmt.Println("Current listening addresses:")
			for _, addr := range tevt.Current {
				fmt.Printf("\t%s: %s\n", fmtAddrAction(addr.Action), addr.Address)
			}
		default:
			fmt.Println("Unknown event:", evt)
		}
	}
}

func fmtAddrAction(action event.AddrAction) string {
	switch action {
	case event.Unknown:
		return "unknown"
	case event.Added:
		return "added"
	case event.Maintained:
		return "maintained"
	case event.Removed:
		return "removed"
	default:
		panic(fmt.Sprintln("Unknown action", action))
	}
}
