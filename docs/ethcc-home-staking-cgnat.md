# EthCC[9] Talk: Home Staking behind CGNAT

**Speaker:** Yorick Downe (Ethstaker)
**Conference:** EthCC[9], Cannes, April 1, 2026
**Stage:** Hepburn Stage (EthStaker track), 11:55–12:15
**Link:** https://ethcc.io/ethcc-9/agenda/home-staking-behind-cgnat-latam-asia-pacific-and-everywhere

---

## Relevance to AutoNAT Project

This talk directly addresses the same problem space as our AutoNAT v2
research: **CGNAT prevents inbound connections to home nodes**. While our
project investigates how libp2p's AutoNAT v2 *detects* NAT reachability,
this talk proposes a complementary approach: **avoid NAT entirely using
IPv6 dual-stack**.

### Key Points

1. **CGNAT is expanding** — not just LATAM/APAC/Africa but increasingly
   Europe and North America. ISPs deploy CGNAT for IPv4 exhaustion,
   giving residential users symmetric NAT (ADPM+APDF), which is the
   hardest NAT type for libp2p hole punching and AutoNAT v2.

2. **IPv6 dual-stack as workaround** — Many ISPs that deploy CGNAT for
   IPv4 simultaneously offer native IPv6 (it's cheaper than CGNAT).
   With IPv6, every device gets a globally routable address — no NAT
   traversal needed. This doesn't "solve" NAT; it sidesteps it by using
   a protocol that doesn't need NAT.

3. **Ethereum client support** — The talk covers current CL/EL client
   support for dual-stack (IPv4+IPv6) operation and configuration
   guidance for home stakers.

### Connection to Our Findings

| Our finding | How IPv6 dual-stack relates |
|-------------|---------------------------|
| **Symmetric NAT silent failure** (Finding 5) | IPv6 bypasses symmetric NAT entirely — no mapping/filtering to test |
| **ADF false positive** (Finding 3) | Not applicable on IPv6 — no filtering behavior |
| **UDP black hole** (Finding 2) | Some ISPs block UDP on IPv4 but allow it on IPv6; needs measurement |
| **CGNAT prevalence** (Future Work) | IPv6 adoption rate is the complementary metric — "what % of CGNAT users have IPv6 available?" |

### Implications for Future Work

Our Tier 2 NAT classification proposal (ants-watch) should also measure:

- **IPv6 adoption rate** among libp2p peers (Nebula already stores
  multiaddresses — query for `/ip6/` addresses)
- **Dual-stack vs IPv4-only vs IPv6-only** distribution
- **IPv6 reachability** — does having an IPv6 address mean it's actually
  dialable? (firewall rules, router misconfig, etc.)

If IPv6 adoption is high among CGNAT users, recommending dual-stack
configuration may be more impactful than fixing AutoNAT v2 edge cases
for symmetric NAT.

### For the Report

This talk validates our finding that CGNAT/symmetric NAT is a growing
real-world problem. It also introduces an alternative mitigation strategy
(IPv6) that our report should acknowledge in the Recommendations or
Future Work sections.
