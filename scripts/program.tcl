# Arguments are passed by subprocess argv, never substituted into Tcl source.
proc run {} {
    global argv
    lassign $argv operation image target flash_part
    open_hw_manager
    connect_hw_server -url localhost:3121
    open_hw_target $target
    set devices [get_hw_devices xc7a35t_0]
    if {[llength $devices] != 1} {error "Expected exactly one XC7A35T"}
    set dev [lindex $devices 0]
    current_hw_device $dev
    refresh_hw_device $dev
    if {$operation eq "load"} {
        set_property PROGRAM.FILE $image $dev
        program_hw_devices $dev
    } elseif {$operation eq "flash"} {
        set parts [get_cfgmem_parts $flash_part]
        if {[llength $parts] != 1} {error "Unrecognized configuration-memory part"}
        create_hw_cfgmem -hw_device $dev -mem_dev [lindex $parts 0]
        set mem [get_property PROGRAM.HW_CFGMEM $dev]
        set_property PROGRAM.ADDRESS_RANGE use_file $mem
        set_property PROGRAM.BIN_OFFSET 0 $mem
        set_property PROGRAM.FILES [list $image] $mem
        set_property PROGRAM.UNUSED_PIN_TERMINATION pull-none $mem
        set_property PROGRAM.BLANK_CHECK 0 $mem
        set_property PROGRAM.ERASE 1 $mem
        set_property PROGRAM.CFG_PROGRAM 1 $mem
        set_property PROGRAM.VERIFY 1 $mem
        create_hw_bitstream -hw_device $dev [get_property PROGRAM.HW_CFGMEM_BITFILE $dev]
        program_hw_devices $dev
        program_hw_cfgmem $mem
        if {[boot_hw_device -timeout 30 $dev] != 1} {error "SPI boot did not assert DONE"}
    } elseif {$operation eq "boot"} {
        if {[boot_hw_device -timeout 30 $dev] != 1} {error "SPI boot did not assert DONE"}
    } else {
        error "Unknown programming operation"
    }
    refresh_hw_device $dev
    puts "CONFIGURATION_STATUS: [get_property REGISTER.CONFIG_STATUS $dev]"
    close_hw_manager
}
if {[catch {run} message]} {
    puts stderr "Programming failed: $message"
    exit 1
}
exit 0
