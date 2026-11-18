"""
KiCad schematic netlist extraction utilities.
"""
import os
import re
from typing import Any, Dict, List
from collections import defaultdict
import logging

_log = logging.getLogger(__name__)  # stdout carries the MCP protocol: never print()

class SchematicParser:
    """Parser for KiCad schematic files to extract netlist information."""
    
    def __init__(self, schematic_path: str):
        """Initialize the schematic parser.
        
        Args:
            schematic_path: Path to the KiCad schematic file (.kicad_sch)
        """
        self.schematic_path = schematic_path
        self.content = ""
        self.components = []
        self.labels = []
        self.wires = []
        self.junctions = []
        self.no_connects = []
        self.power_symbols = []
        self.hierarchical_labels = []
        self.global_labels = []
        
        # Netlist information
        self.nets = defaultdict(list)  # Net name -> connected pins
        self.component_pins = {}  # (component_ref, pin_num) -> net_name
        
        # Component information
        self.component_info = {}  # component_ref -> component details
        
        # Load the file
        self._load_schematic()

    def _load_schematic(self) -> None:
        """Load the schematic file content."""
        if not os.path.exists(self.schematic_path):
            _log.info(f"Schematic file not found: {self.schematic_path}")
            raise FileNotFoundError(f"Schematic file not found: {self.schematic_path}")
        
        try:
            with open(self.schematic_path, 'r') as f:
                self.content = f.read()
                _log.info(f"Successfully loaded schematic: {self.schematic_path}")
        except Exception as e:
            _log.info(f"Error reading schematic file: {str(e)}")
            raise

    def parse(self) -> Dict[str, Any]:
        """Parse the schematic to extract netlist information.
        
        Returns:
            Dictionary with parsed netlist information
        """
        _log.info("Starting schematic parsing")
        
        # Extract symbols (components)
        self._extract_components()
        
        # Extract wires
        self._extract_wires()
        
        # Extract junctions
        self._extract_junctions()
        
        # Extract labels
        self._extract_labels()
        
        # Extract power symbols
        self._extract_power_symbols()
        
        # Extract no-connects
        self._extract_no_connects()
        
        # Build netlist
        self._build_netlist()
        
        # Create result
        result = {
            "components": self.component_info,
            "nets": dict(self.nets),
            "labels": self.labels,
            "wires": self.wires,
            "junctions": self.junctions,
            "power_symbols": self.power_symbols,
            "component_count": len(self.component_info),
            "net_count": len(self.nets)
        }
        
        _log.info(f"Schematic parsing complete: found {len(self.component_info)} components and {len(self.nets)} nets")
        return result

    def _extract_s_expressions(self, pattern: str) -> List[str]:
        """Extract all matching S-expressions from the schematic content.
        
        Args:
            pattern: Regex pattern to match the start of S-expressions
            
        Returns:
            List of matching S-expressions
        """
        matches = []
        positions = []
        
        # Find all starting positions of matches
        for match in re.finditer(pattern, self.content):
            positions.append(match.start())
        
        # Extract full S-expressions for each match
        for pos in positions:
            # Start from the matching position
            current_pos = pos
            depth = 0
            s_exp = ""
            
            # Extract the full S-expression by tracking parentheses
            while current_pos < len(self.content):
                char = self.content[current_pos]
                s_exp += char
                
                if char == '(':
                    depth += 1
                elif char == ')':
                    depth -= 1
                    if depth == 0:
                        # Found the end of the S-expression
                        break
                
                current_pos += 1
            
            matches.append(s_exp)
        
        return matches

    def _extract_components(self) -> None:
        """Extract component information from schematic."""
        _log.info("Extracting components")
        
        # Extract all symbol expressions (components)
        symbols = self._extract_s_expressions(r'\(symbol\s+')
        
        for symbol in symbols:
            component = self._parse_component(symbol)
            if component:
                self.components.append(component)
                
                # Add to component info dictionary
                ref = component.get('reference', 'Unknown')
                self.component_info[ref] = component
        
        _log.info(f"Extracted {len(self.components)} components")

    def _parse_component(self, symbol_expr: str) -> Dict[str, Any]:
        """Parse a component from a symbol S-expression.
        
        Args:
            symbol_expr: Symbol S-expression
            
        Returns:
            Component information dictionary
        """
        component = {}
        
        # Extract library component ID
        lib_id_match = re.search(r'\(lib_id\s+"([^"]+)"\)', symbol_expr)
        if lib_id_match:
            component['lib_id'] = lib_id_match.group(1)
        
        # Extract reference (e.g., R1, C2)
        property_matches = re.finditer(r'\(property\s+"([^"]+)"\s+"([^"]+)"', symbol_expr)
        for match in property_matches:
            prop_name = match.group(1)
            prop_value = match.group(2)
            
            if prop_name == "Reference":
                component['reference'] = prop_value
            elif prop_name == "Value":
                component['value'] = prop_value
            elif prop_name == "Footprint":
                component['footprint'] = prop_value
            else:
                # Store other properties
                if 'properties' not in component:
                    component['properties'] = {}
                component['properties'][prop_name] = prop_value
        
        # Extract position
        pos_match = re.search(r'\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)', symbol_expr)
        if pos_match:
            component['position'] = {
                'x': float(pos_match.group(1)),
                'y': float(pos_match.group(2)),
                'angle': float(pos_match.group(3).strip() if pos_match.group(3) else 0)
            }
        
        # Extract pins
        pins = []
        pin_matches = re.finditer(r'\(pin\s+\(num\s+"([^"]+)"\)\s+\(name\s+"([^"]+)"\)', symbol_expr)
        for match in pin_matches:
            pin_num = match.group(1)
            pin_name = match.group(2)
            pins.append({
                'num': pin_num,
                'name': pin_name
            })
        
        if pins:
            component['pins'] = pins
        
        return component

    def _extract_wires(self) -> None:
        """Extract wire information from schematic."""
        _log.info("Extracting wires")
        
        # Extract all wire expressions
        wires = self._extract_s_expressions(r'\(wire\s+')
        
        for wire in wires:
            # Extract the wire coordinates
            pts_match = re.search(r'\(pts\s+\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\s+\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\)', wire)
            if pts_match:
                self.wires.append({
                    'start': {
                        'x': float(pts_match.group(1)),
                        'y': float(pts_match.group(2))
                    },
                    'end': {
                        'x': float(pts_match.group(3)),
                        'y': float(pts_match.group(4))
                    }
                })
        
        _log.info(f"Extracted {len(self.wires)} wires")

    def _extract_junctions(self) -> None:
        """Extract junction information from schematic."""
        _log.info("Extracting junctions")
        
        # Extract all junction expressions
        junctions = self._extract_s_expressions(r'\(junction\s+')
        
        for junction in junctions:
            # Extract the junction coordinates
            xy_match = re.search(r'\(junction\s+\(xy\s+([\d\.-]+)\s+([\d\.-]+)\)\)', junction)
            if xy_match:
                self.junctions.append({
                    'x': float(xy_match.group(1)),
                    'y': float(xy_match.group(2))
                })
        
        _log.info(f"Extracted {len(self.junctions)} junctions")

    def _extract_labels(self) -> None:
        """Extract label information from schematic."""
        _log.info("Extracting labels")
        
        # Extract local labels
        local_labels = self._extract_s_expressions(r'\(label\s+')
        
        for label in local_labels:
            # Extract label text and position
            label_match = re.search(r'\(label\s+"([^"]+)"\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)', label)
            if label_match:
                self.labels.append({
                    'type': 'local',
                    'text': label_match.group(1),
                    'position': {
                        'x': float(label_match.group(2)),
                        'y': float(label_match.group(3)),
                        'angle': float(label_match.group(4).strip() if label_match.group(4) else 0)
                    }
                })
        
        # Extract global labels
        global_labels = self._extract_s_expressions(r'\(global_label\s+')
        
        for label in global_labels:
            # Extract global label text and position
            label_match = re.search(r'\(global_label\s+"([^"]+)"\s+\(shape\s+([^\s\)]+)\)\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)', label)
            if label_match:
                self.global_labels.append({
                    'type': 'global',
                    'text': label_match.group(1),
                    'shape': label_match.group(2),
                    'position': {
                        'x': float(label_match.group(3)),
                        'y': float(label_match.group(4)),
                        'angle': float(label_match.group(5).strip() if label_match.group(5) else 0)
                    }
                })
        
        # Extract hierarchical labels
        hierarchical_labels = self._extract_s_expressions(r'\(hierarchical_label\s+')
        
        for label in hierarchical_labels:
            # Extract hierarchical label text and position
            label_match = re.search(r'\(hierarchical_label\s+"([^"]+)"\s+\(shape\s+([^\s\)]+)\)\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)', label)
            if label_match:
                self.hierarchical_labels.append({
                    'type': 'hierarchical',
                    'text': label_match.group(1),
                    'shape': label_match.group(2),
                    'position': {
                        'x': float(label_match.group(3)),
                        'y': float(label_match.group(4)),
                        'angle': float(label_match.group(5).strip() if label_match.group(5) else 0)
                    }
                })
        
        _log.info(f"Extracted {len(self.labels)} local labels, {len(self.global_labels)} global labels, and {len(self.hierarchical_labels)} hierarchical labels")

    def _extract_power_symbols(self) -> None:
        """Extract power symbol information from schematic."""
        _log.info("Extracting power symbols")
        
        # Extract all power symbol expressions
        power_symbols = self._extract_s_expressions(r'\(symbol\s+\(lib_id\s+"power:')
        
        for symbol in power_symbols:
            # Extract power symbol type and position
            type_match = re.search(r'\(lib_id\s+"power:([^"]+)"\)', symbol)
            pos_match = re.search(r'\(at\s+([\d\.-]+)\s+([\d\.-]+)(\s+[\d\.-]+)?\)', symbol)
            
            if type_match and pos_match:
                self.power_symbols.append({
                    'type': type_match.group(1),
                    'position': {
                        'x': float(pos_match.group(1)),
                        'y': float(pos_match.group(2)),
                        'angle': float(pos_match.group(3).strip() if pos_match.group(3) else 0)
                    }
                })
        
        _log.info(f"Extracted {len(self.power_symbols)} power symbols")

    def _extract_no_connects(self) -> None:
        """Extract no-connect information from schematic."""
        _log.info("Extracting no-connects")
        
        # Extract all no-connect expressions
        no_connects = self._extract_s_expressions(r'\(no_connect\s+')
        
        for no_connect in no_connects:
            # Extract the no-connect coordinates
            xy_match = re.search(r'\(no_connect\s+\(at\s+([\d\.-]+)\s+([\d\.-]+)\)', no_connect)
            if xy_match:
                self.no_connects.append({
                    'x': float(xy_match.group(1)),
                    'y': float(xy_match.group(2))
                })
        
        _log.info(f"Extracted {len(self.no_connects)} no-connects")

    def _build_netlist(self) -> None:
        """Build the netlist from extracted components and connections."""
        _log.info("Building netlist from schematic data")
        
        # TODO: Implement netlist building algorithm
        # This is a complex task that involves:
        # 1. Tracking connections between components via wires
        # 2. Handling labels (local, global, hierarchical)
        # 3. Processing power symbols
        # 4. Resolving junctions
        
        # For now, we'll implement a basic version that creates a list of nets
        # based on component references and pin numbers
        
        # Process global labels as nets
        for label in self.global_labels:
            net_name = label['text']
            self.nets[net_name] = []  # Initialize empty list for this net
        
        # Process power symbols as nets
        for power in self.power_symbols:
            net_name = power['type']
            if net_name not in self.nets:
                self.nets[net_name] = []
        
        # In a full implementation, we would now trace connections between
        # components, but that requires a more complex algorithm to follow wires
        # and detect connected pins
        
        # For demonstration, we'll add a placeholder note
        _log.info("Note: Full netlist building requires complex connectivity tracing")
        _log.info(f"Found {len(self.nets)} potential nets from labels and power symbols")


def _find_kicad_cli() -> str:
    """Locate kicad-cli: kicad_env resolution (env, per-machine config, discovery), then PATH."""
    import shutil
    from kicad_mcp.adaptive_editor import kicad_env
    try:
        resolved = kicad_env.kicad_cli()
        if resolved.is_file():
            return str(resolved)
    except kicad_env.KiCadNotFound:
        pass
    found = shutil.which("kicad-cli")
    return found or ""


def extract_netlist_cli(schematic_path: str) -> Dict[str, Any]:
    """Extract the netlist via kicad-cli — eeschema's OWN netlister.

    Authoritative: real connectivity rules (label scoping, hierarchy, buses,
    power symbols), unlike the regex fallback parser in this module. Raises on
    any failure so the caller can fall back.
    """
    import subprocess
    import tempfile
    import defusedxml.ElementTree as ET  # no entity expansion on file content

    cli = _find_kicad_cli()
    if not cli:
        raise RuntimeError("kicad-cli not found")

    with tempfile.TemporaryDirectory() as td:
        out_xml = os.path.join(td, "netlist.xml")
        proc = subprocess.run(
            [cli, "sch", "export", "netlist", "--format", "kicadxml",
             "-o", out_xml, schematic_path],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not os.path.exists(out_xml):
            raise RuntimeError(
                f"kicad-cli netlist export failed: {proc.stderr.strip()[:400]}")
        root = ET.parse(out_xml).getroot()

    components: Dict[str, Any] = {}
    for comp in root.findall("./components/comp"):
        ref = comp.get("ref", "")
        if not ref:
            continue
        libsource = comp.find("libsource")
        components[ref] = {
            "reference": ref,
            "value": (comp.findtext("value") or "").strip(),
            "footprint": (comp.findtext("footprint") or "").strip(),
            "lib_id": (f"{libsource.get('lib', '')}:{libsource.get('part', '')}"
                       if libsource is not None else ""),
        }

    nets: Dict[str, List[str]] = {}
    for net in root.findall("./nets/net"):
        name = net.get("name", "")
        pins = [f"{node.get('ref')}-{node.get('pin')}" for node in net.findall("node")]
        if name:
            nets[name] = pins

    return {
        "components": components,
        "nets": nets,
        "component_count": len(components),
        "net_count": len(nets),
        "source": "kicad-cli",
    }


def extract_netlist(schematic_path: str) -> Dict[str, Any]:
    """Extract netlist information from a KiCad schematic file.

    Tries the authoritative kicad-cli netlister first (correct connectivity,
    hierarchy-aware); falls back to the legacy regex parser only if the CLI
    is unavailable or fails.

    Args:
        schematic_path: Path to the KiCad schematic file (.kicad_sch)

    Returns:
        Dictionary with netlist information
    """
    try:
        return extract_netlist_cli(schematic_path)
    except Exception as cli_err:
        _log.info(f"kicad-cli netlist path failed ({cli_err}); "
              f"falling back to regex parser")
    try:
        parser = SchematicParser(schematic_path)
        result = parser.parse()
        result["source"] = "regex-fallback"
        return result
    except Exception as e:
        _log.info(f"Error extracting netlist: {str(e)}")
        return {
            "error": str(e),
            "components": {},
            "nets": {},
            "component_count": 0,
            "net_count": 0
        }


def analyze_netlist(netlist_data: Dict[str, Any]) -> Dict[str, Any]:
    """Analyze netlist data to provide insights.
    
    Args:
        netlist_data: Dictionary with netlist information
        
    Returns:
        Dictionary with analysis results
    """
    results = {
        "component_count": netlist_data.get("component_count", 0),
        "net_count": netlist_data.get("net_count", 0),
        "component_types": defaultdict(int),
        "power_nets": []
    }
    
    # Analyze component types
    for ref, component in netlist_data.get("components", {}).items():
        # Extract component type from reference (e.g., R1 -> R)
        comp_type = re.match(r'^([A-Za-z_]+)', ref)
        if comp_type:
            results["component_types"][comp_type.group(1)] += 1
    
    # Identify power nets
    for net_name in netlist_data.get("nets", {}):
        if any(net_name.startswith(prefix) for prefix in ["VCC", "VDD", "GND", "+5V", "+3V3", "+12V"]):
            results["power_nets"].append(net_name)
    
    # Count pin connections
    total_pins = sum(len(pins) for pins in netlist_data.get("nets", {}).values())
    results["total_pin_connections"] = total_pins
    
    return results
