# Read device identity/configuration status without programming FPGA or flash.
open_hw_manager
connect_hw_server -url localhost:3121
puts "TARGETS=[get_hw_targets]"
open_hw_target
foreach dev [get_hw_devices] {
    puts "DEVICE=$dev PART=[get_property PART $dev] IDCODE=[get_property IDCODE_HEX $dev]"
    report_property $dev
}
close_hw_target
disconnect_hw_server
close_hw_manager
exit
