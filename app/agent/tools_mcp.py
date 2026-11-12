# -*- coding: utf-8 -*-




def _t_mcp_list_servers(root, args, allow_outside=False):
    from app import mcp
    return mcp.list_servers_text(), None


def _t_mcp_list_tools(root, args, allow_outside=False):
    from app import mcp
    return mcp.list_tools_text((args or {}).get("server")), None


def _t_mcp_list_resources(root, args, allow_outside=False):
    from app import mcp
    return mcp.list_resources_text((args or {}).get("server")), None


def _t_mcp_read_resource(root, args, allow_outside=False):
    from app import mcp
    a = args or {}
    return mcp.read_resource_text(a.get("server"), a.get("uri")), None
