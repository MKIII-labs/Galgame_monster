# -*- coding: utf-8 -*-




def _t_mcp_list_servers(root, args, allow_outside=False):
    from app import mcp
    return mcp.list_servers_text(), None


def _t_mcp_list_tools(root, args, allow_outside=False):
    from app import mcp
    return mcp.list_tools_text((args or {}).get("server")), None
